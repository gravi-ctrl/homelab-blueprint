#!/usr/bin/env python3

# @DESCRIPTION: Executes commands on Linux/Windows with Telegram alerts (fail/success/all/mute), an optional timeout kill, and fallback logging on delivery failure.
# @FREQUENCY: Varies
# @USES_ENV: TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user, root

# optional: sudo ln -s cron-guard.py /usr/local/bin/cron-guard

# USAGE:
# python cron-guard.py --mode fail "My Backup" "bash backup.sh"               (Only notify if it breaks)
# python cron-guard.py --mode success "Health Check" "curl ..."               (Only notify if it works)
# python cron-guard.py --mode all "Weekly Sync" "rsync -av ..."               (Always notify)
# python cron-guard.py --mode mute "Quiet Job" "some_command"                 (Never notify; just for clean naming/logging)
#                                                                             (Writes a local heartbeat file at ./status/*.json per job)
# python cron-guard.py --mode fail --timeout 2700 "Backup" "bash backup.sh"   (Any mode kills it; alert only fires on fail/all, per --mode)
#
# WHAT IT DOES (all modes):
# - Runs the command, streaming + capturing combined stdout/stderr
# - Sends a Telegram alert per --mode (success/failure/killed/timeout), with 3x retry on delivery
# - --timeout N: kills the job if it's still running after N seconds, reported as its own distinct status
# - On Telegram failure: logs to stderr and appends to failed_alerts.log
# - If the Telegram token/chat ID are missing or unset: skips the retry entirely and writes MISSING_TELEGRAM_CREDENTIALS.log instead
# - Keeps a full log under logs/ only when an alert fires AND output is large (>15 lines or >3000 chars), linked in the alert
# - Forwards SIGTERM/SIGINT to the child process tree for a clean shutdown on kill/reboot or --timeout (kills the whole tree, not just the shell, so real children like apt/rsync can't outlive the kill)
# - mute mode additionally writes status/<job>.json (last run time, exit code, duration, timed_out) — a local heartbeat for manual checks only; never sent or read by anything else
# - Per-job log/status filenames include a short hash of the job name, so two differently-named jobs never collide on disk

import sys
import os
import subprocess
import urllib.request
import urllib.parse
import datetime
import time
import html
import collections
import argparse
import signal
import re
import json
import threading
import hashlib

def kill_process_tree(proc):
    """Kill the whole process tree, not just the shell handle cron-guard holds.
    shell=True means `proc` is /bin/sh (or cmd.exe on Windows) -- terminate()-ing
    just that handle kills the shell but leaves real children (e.g. apt-get)
    orphaned and still running to completion. This kills the whole group/tree.
    """
    if not proc:
        return
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass

def load_dotenv(filepath):
    if not os.path.isfile(filepath): return
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            if '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip().strip('"\'')

def write_heartbeat(script_dir, safe_job_name, job_name, exit_code, duration_seconds, timed_out):
    # Local-only heartbeat for mute mode: never sent anywhere, never read by the
    # dashboard. Just a small file you can `cat` to check a quiet job actually ran.
    status_dir = os.path.join(script_dir, "status")
    try:
        os.makedirs(status_dir, exist_ok=True)
        status_path = os.path.join(status_dir, f"{safe_job_name}.json")
        payload = {
            "job_name": job_name,
            "last_run": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "exit_code": exit_code,
            "duration_seconds": duration_seconds,
            "timed_out": timed_out,
        }
        with open(status_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        # Best-effort only: never let a heartbeat write failure affect the job's exit code
        print(f"[cron-guard] WARNING: Could not write heartbeat file: {e}", file=sys.stderr)

def send_telegram_alert(token, chat_id, job_name, exit_code, log_tail, duration, total_lines, full_log_path, timed_out=False, creds_missing=False, timeout_limit=None, is_syntax_error=False, skip_missing_creds_log=False):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    if timed_out:
        status_header = "🕐 <b>TASK TIMEOUT</b>"
        status_label = f"Exceeded {timeout_limit}s limit" if timeout_limit else "Exceeded configured timeout"
    elif exit_code == 0:
        status_header = "✅ <b>TASK SUCCESSFUL</b>"
        status_label = "Success"
    elif exit_code in (-15, 143, 130, -2): # Standard SIGTERM/SIGINT codes (Linux & Windows)
        status_header = "🛑 <b>TASK KILLED</b>"
        status_label = "Terminated by OS/User"
    elif is_syntax_error:
        status_header = "⚠️ <b>CRON-GUARD MISCONFIGURED</b>"
        status_label = "Wrapper Syntax Error"
    else:
        status_header = "🚨 <b>TASK FAILURE</b>"
        status_label = f"Failed (Code {exit_code})"

    truncated_msg = ""
    max_log_chars = 3000

    # Format message dynamically based on if we kept a log file
    if full_log_path:
        if total_lines > 15:
            truncated_msg += f"\n⚠️ <i>Showing last 15 of {total_lines} lines.</i>"
        if len(log_tail) > max_log_chars:
            truncated_msg += f"\n⚠️ <i>Output exceeded character limit.</i>"
            log_tail = "...[TRUNCATED BY CHAR LIMIT]\n" + log_tail[-max_log_chars:]

        if truncated_msg:
            truncated_msg += f"\n📁 <b>Full Log:</b> {html.escape(full_log_path)}\n"
    else:
        # No file kept, but we must protect TG API
        if len(log_tail) > max_log_chars:
            log_tail = "...[TRUNCATED BY CHAR LIMIT]\n" + log_tail[-max_log_chars:]

    text = (
        f"{status_header}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📂 <b>Job:</b> {html.escape(job_name)}\n"
        f"📊 <b>Status:</b> {status_label}\n"
        f"⏱️ <b>Duration:</b> {duration}\n"
        f"⏰ <b>Finished:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{truncated_msg}"
        f"📜 <b>Message / Logs:</b>\n<pre>{html.escape(log_tail)}</pre>"
    )

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "text": text
    }).encode('utf-8')

    last_error = None
    if creds_missing:
        last_error = "Telegram token/chat ID missing or unset (.env not found or using placeholder values) — skipped delivery attempt"
    else:
        for _ in range(3):
            try:
                req = urllib.request.Request(url, data=data, method='POST')
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.getcode() == 200: return True
            except Exception as e:
                last_error = str(e)
                time.sleep(2)

    # --- Dual fallback logging (stderr + file) ---
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    alert_msg = (
        f"[cron-guard] WARNING: Telegram alert delivery failed\n"
        f"  Job      : {job_name}\n"
        f"  Exit code: {exit_code}\n"
        f"  Duration : {duration}\n"
        f"  Finished : {timestamp}\n"
        f"  Error    : {last_error}\n"
    )

    print(alert_msg, file=sys.stderr)

    if creds_missing and skip_missing_creds_log:
        return False

    try:
        script_dir = os.path.dirname(os.path.realpath(__file__))
        fallback_filename = 'MISSING_TELEGRAM_CREDENTIALS.log' if creds_missing else 'failed_alerts.log'
        fallback_log = os.path.join(script_dir, fallback_filename)
        # MISSING_TELEGRAM_CREDENTIALS.log is a current-state flag (overwrite each
        # time) -- failed_alerts.log is a real history of delivery failures (append).
        file_mode = 'w' if creds_missing else 'a'
        with open(fallback_log, file_mode, encoding='utf-8') as f:
            f.write(
                f"{timestamp} | job={job_name!r} | exit={exit_code} | duration={duration} | error={last_error}\n"
                f"--- last logs ---\n{log_tail}\n-----------------\n"
            )
    except Exception as log_err:
        print(f"[cron-guard] WARNING: Could not write to {fallback_filename}: {log_err}", file=sys.stderr)

    return False


class AlertingArgumentParser(argparse.ArgumentParser):
    """Custom parser to intercept arguments errors and send a Telegram 'curse' alert."""
    def error(self, message):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        load_dotenv(os.path.join(script_dir, '.env'))

        token = os.environ.get("TELEGRAM_DANTE_BOT_TOKEN", "YOUR_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_ID")

        creds_missing = (not token or token == "YOUR_TOKEN" or not chat_id or chat_id == "YOUR_ID")

        curse_msg = (
            f"Close! You messed up the cron-guard syntax!\n\n"
            f"Error: {message}\n\n"
            f"Go fix your damn thing!"
        )

        send_telegram_alert(
            token=token,
            chat_id=chat_id,
            job_name="cron-guard Syntax Error",
            exit_code=2,
            log_tail=curse_msg,
            duration="0:00:00",
            total_lines=0,
            full_log_path=None,
            creds_missing=creds_missing,
            is_syntax_error=True
        )

        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")

def main():
    parser = AlertingArgumentParser(description="Run a command and notify via Telegram.")
    parser.add_argument("--mode", choices=["fail", "success", "all", "mute"], default="fail",
                        help="When to send notification (default: fail)")
    parser.add_argument("--timeout", type=int, default=None,
                        help="Kill the job if it's still running after this many seconds (default: no limit)")
    parser.add_argument("job_name", help="Name of the job for the report")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="The command to execute")

    args = parser.parse_args()

    # Intercept missing command silently allowed by REMAINDER
    if not args.command:
        parser.error("You forgot to provide the actual command to run after the job name!")

    if len(args.command) == 1:
        full_cmd = args.command[0]
    else:
        if os.name == 'nt':
            full_cmd = subprocess.list2cmdline(args.command)
        else:
            import shlex
            full_cmd = shlex.join(args.command)

    script_dir = os.path.dirname(os.path.realpath(__file__))
    load_dotenv(os.path.join(script_dir, '.env'))

    token = os.environ.get("TELEGRAM_DANTE_BOT_TOKEN", "YOUR_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_ID")
    creds_missing = (not token or token == "YOUR_TOKEN" or not chat_id or chat_id == "YOUR_ID")

    if creds_missing:
        print(f"⚠️ [cron-guard] WARNING: .env is missing or incomplete! Alerts for '{args.job_name}' cannot be sent.", file=sys.stderr)
        try:
            pre_flight_log = os.path.join(script_dir, 'MISSING_TELEGRAM_CREDENTIALS.log')
            with open(pre_flight_log, 'w', encoding='utf-8') as f:
                timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                f.write(f"{timestamp} | [CONFIG WARNING] job={args.job_name!r} started, but Telegram credentials are missing!\n")
        except Exception:
            pass

    start_time = time.monotonic()
    log_queue = collections.deque(maxlen=15)
    total_lines = 0

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONUNBUFFERED"] = "1"

    process = None
    timed_out_flag = {"value": False}

    def forward_signal(signum, frame):
        if process:
            log_queue.append(f"⚠️ [cron-guard] Received Signal {signum}, killing child process tree...")
            kill_process_tree(process)

    def timeout_kill():
        if process:
            timed_out_flag["value"] = True
            log_queue.append(f"🕐 [cron-guard] Timeout of {args.timeout}s exceeded, killing child process tree...")
            kill_process_tree(process)

    try:
        signal.signal(signal.SIGTERM, forward_signal)
        signal.signal(signal.SIGINT, forward_signal)
    except ValueError:
        pass

    name_hash = hashlib.md5(args.job_name.encode('utf-8')).hexdigest()[:6]
    safe_job_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', args.job_name) + "_" + name_hash
    log_dir = os.path.join(script_dir, "logs")
    full_log_path = os.path.join(log_dir, f"{safe_job_name}_latest.log")
    log_file_handle = None

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file_handle = open(full_log_path, 'w', encoding='utf-8')
    except Exception:
        full_log_path = None

    timer = None
    try:
        popen_kwargs = dict(
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=child_env
        )
        if os.name == 'nt':
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # New process group only: killpg can still target just this job's tree without hitting cron-guard itself. This does not detach the controlling terminal -- so `sudo` can still prompt/authenticate over /dev/tty.
            popen_kwargs['preexec_fn'] = os.setpgrp

        process = subprocess.Popen(full_cmd, **popen_kwargs)

        if args.timeout:
            timer = threading.Timer(args.timeout, timeout_kill)
            timer.daemon = True
            timer.start()

        for line in iter(process.stdout.readline, ''):
            clean_line = line.rstrip('\n')
            print(clean_line)

            if log_file_handle:
                log_file_handle.write(clean_line + '\n')
                log_file_handle.flush()

            log_queue.append(clean_line)
            total_lines += 1

        exit_code = process.wait()
    except Exception as e:
        exit_code = 1
        log_queue.append(f"Wrapper Execution Error: {str(e)}")
    finally:
        if timer:
            timer.cancel()
        # Guaranteed closure of file stream to unlock it for Windows deletion
        if log_file_handle and not log_file_handle.closed:
            log_file_handle.close()

    duration_seconds = int(time.monotonic() - start_time)
    duration_str = str(datetime.timedelta(seconds=duration_seconds))

    if args.mode == "mute":
        write_heartbeat(script_dir, safe_job_name, args.job_name, exit_code, duration_seconds, timed_out_flag["value"])

    should_send = False
    if args.mode == "all":
        should_send = True
    elif args.mode == "fail" and exit_code != 0:
        should_send = True
    elif args.mode == "success" and exit_code == 0:
        should_send = True

    # Check if we should keep the log file
    log_tail = "\n".join(log_queue) or "No output."
    keep_file = False

    if should_send:
        # Keep file ONLY if message is sent AND limits are exceeded
        if total_lines > 15 or len(log_tail) > 3000:
            keep_file = True

    # Delete the file cleanly if we don't need it
    if not keep_file and full_log_path and os.path.exists(full_log_path):
        try:
            os.remove(full_log_path)
        except OSError:
            pass

    # Send the Telegram message
    if should_send:
        final_log_path = full_log_path if keep_file else None
        send_telegram_alert(
            token, chat_id, args.job_name, exit_code, log_tail, duration_str, total_lines, final_log_path,
            timed_out=timed_out_flag["value"], creds_missing=creds_missing, timeout_limit=args.timeout,
            skip_missing_creds_log=True
        )

    sys.exit(exit_code)

if __name__ == "__main__":
    main()