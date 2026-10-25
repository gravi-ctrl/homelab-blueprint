#!/usr/bin/env python3

# @DESCRIPTION: Executes commands on Linux/Windows with Telegram alerts (fail/success/all/mute), an optional timeout kill, and fallback logging on delivery failure.
# @FREQUENCY: Varies
# @USES_ENV: TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user, root
# @CAPABILITIES:
#   • Kills entire process trees on timeout, preventing orphaned child jobs from draining resources.
#   • Retries Telegram delivery three times, logging failures locally to keep a reliable audit trail.
#   • Mute mode writes silent JSON heartbeats, giving instant status snapshots without alert noise.
#   • Auto‑adjusts file ownership for root or sudo runs, ensuring correct permissions across users.
#   • Stores full logs only when alerts fire and output exceeds thresholds, keeping disk usage lean.
#   • Hashes job names for unique file identifiers, avoiding disk collisions between similarly named tasks.
#   • Suppresses repeated identical failures for a configurable period, sending a single reminder instead of noise.
#   • Sends a single recovery message after a previously alerted failure resolves, closing the alert loop.

# optional: sudo ln -s cron-guard.py /usr/local/bin/cron-guard

# USAGE:
# python cron-guard.py --mode fail "My Backup" "bash backup.sh"               (Only notify if it breaks)
# python cron-guard.py --mode success "Health Check" "curl ..."               (Only notify if it works)
# python cron-guard.py --mode all "Weekly Sync" "rsync -av ..."               (Always notify)
# python cron-guard.py --mode mute "Quiet Job" "some_command"                 (Never notify; just for clean naming/logging)
#                                                                             (Writes a local heartbeat file at ./status/*.json per job)
# python cron-guard.py --mode fail --timeout 2700 "Backup" "bash backup.sh"   (Any mode kills it; alert only fires on fail/all, per --mode)
# python cron-guard.py --mode fail --repeat-after 6h "Sync" "bash sync.sh"    (Same error again? silent for 6h. A DIFFERENT error alerts immediately)
# python cron-guard.py --mode fail --repeat-after 1d --recovery "Sync" "..."  (...and sends one "recovered" message when it works again)
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
# - --repeat-after DURATION (e.g. 90, 15m, 6h, 1d; default 0 = alert on every failure): while a job keeps failing with the SAME error
#   (same exit code + same output once digits/timestamps/hex IDs are ignored) alerts are suppressed until DURATION has passed since
#   the last alert, then one reminder ("Still failing: N runs since ...") is sent. A different error always alerts immediately.
#   State lives in state/<job>.json. If delivery fails the failure is NOT counted as alerted, so a Telegram outage never hides errors.
# - --recovery: sends one "recovered" message on the first success after a failure that was actually alerted (fail/all modes only)

import argparse
import collections
import datetime
import hashlib
import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


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

def fix_ownership(path, script_dir):
    """If running as root on Linux, change the path's ownership to either:
    1. The invoking user (if running via sudo)
    2. The owner of the script directory (if running via root cron)
    """
    if os.name == 'nt' or not hasattr(os, 'getuid') or os.getuid() != 0:
        return

    target_uid = None
    target_gid = None

    # Check for sudo env variables
    if 'SUDO_UID' in os.environ and 'SUDO_GID' in os.environ:
        try:
            target_uid = int(os.environ['SUDO_UID'])
            target_gid = int(os.environ['SUDO_GID'])
        except ValueError:
            pass

    # Fallback to the owner of the script directory (useful for root crontab)
    if target_uid is None and os.path.exists(script_dir):
        try:
            stat_info = os.stat(script_dir)
            target_uid = stat_info.st_uid
            target_gid = stat_info.st_gid
        except Exception:
            pass

    # Apply if the target owner is not root
    if target_uid is not None and target_uid != 0:
        try:
            os.chown(path, target_uid, target_gid)
        except Exception:
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
        fix_ownership(status_dir, script_dir)
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
        fix_ownership(status_path, script_dir)
    except Exception as e:
        # Best-effort only: never let a heartbeat write failure affect the job's exit code
        print(f"[cron-guard] WARNING: Could not write heartbeat file: {e}", file=sys.stderr)

def parse_duration(value):
    """'90' / '90s' / '15m' / '6h' / '2d' -> seconds (argparse type)."""
    m = re.fullmatch(r'(\d+)\s*([smhd]?)', str(value).strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration {value!r} (use e.g. 90, 15m, 6h, 1d)")
    return int(m.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2)]

def failure_fingerprint(exit_code, timed_out, log_tail):
    """Stable ID for 'the same failure'. ANSI colours, hex IDs and digits are ignored so
    timestamps, PIDs and counters don't make an identical error look new."""
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', log_tail)
    text = re.sub(r'\b[0-9a-f]{8,}\b', 'H', text, flags=re.IGNORECASE)
    text = re.sub(r'\d+', '#', text)
    key = f"{exit_code}|{int(timed_out)}|{text}"
    return hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest()[:16]

def _fmt_ts(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M')

def decide_failure_alert(state, fingerprint, repeat_after, now):
    """Decide whether this failed run deserves an alert. Returns (send, note, new_state)."""
    same = state.get("fingerprint") == fingerprint
    failures = state.get("failures", 0) + 1 if same else 1
    first = state.get("first_failure", now) if same else now
    last_alert = state.get("last_alert", 0) if same else 0
    suppressed = state.get("suppressed", 0) if same else 0

    due = (not same) or last_alert == 0 or repeat_after == 0 or (now - last_alert) >= repeat_after
    new_state = {"fingerprint": fingerprint, "failures": failures, "first_failure": first,
                 "last_alert": last_alert, "suppressed": suppressed}
    if not due:
        new_state["suppressed"] = suppressed + 1
        return False, "", new_state

    note = ""
    if state.get("fingerprint") and not same:
        note = "Different error than the previous failure"
    elif same and failures > 1:
        note = f"Still failing: {failures} failed runs since {_fmt_ts(first)}"
        if suppressed:
            note += f" ({suppressed} alert(s) suppressed)"
    new_state["last_alert"] = now
    new_state["suppressed"] = 0
    return True, note, new_state

def load_state(state_path):
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def save_state(script_dir, state_path, state):
    # Best-effort: a state write failure must never affect the job's exit code.
    try:
        state_dir = os.path.dirname(state_path)
        os.makedirs(state_dir, exist_ok=True)
        fix_ownership(state_dir, script_dir)
        tmp_path = state_path + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, state_path)
        fix_ownership(state_path, script_dir)
    except Exception as e:
        print(f"[cron-guard] WARNING: Could not write alert state: {e}", file=sys.stderr)

def clear_state(state_path):
    try:
        os.remove(state_path)
    except OSError:
        pass

def send_telegram_alert(token, chat_id, job_name, exit_code, log_tail, duration, total_lines, full_log_path, timed_out=False, creds_missing=False, timeout_limit=None, is_syntax_error=False, skip_missing_creds_log=False, note=""):
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
            truncated_msg += "\n⚠️ <i>Output exceeded character limit.</i>"
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
        f"{f'🔁 <i>{html.escape(note)}</i>' + chr(10) if note else ''}"
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
        fix_ownership(fallback_log, script_dir)
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
    # Force strict 755/644 permissions only on Linux
    if os.name != 'nt':
        os.umask(0o022)

    parser = AlertingArgumentParser(description="Run a command and notify via Telegram.")
    parser.add_argument("--mode", choices=["fail", "success", "all", "mute"], default="fail",
                        help="When to send notification (default: fail)")
    parser.add_argument("--timeout", type=int, default=None,
                        help="Kill the job if it's still running after this many seconds (default: no limit)")
    parser.add_argument("--repeat-after", type=parse_duration, default=0, metavar="DURATION",
                        help="If the job keeps failing with the SAME error, re-alert only after this long (e.g. 90, 15m, 6h, 1d). A different error always alerts immediately. Default 0 = alert on every failure")
    parser.add_argument("--recovery", action="store_true",
                        help="Send one 'recovered' message on the first success after an alerted failure (fail/all modes)")
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
            fix_ownership(pre_flight_log, script_dir)
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
        fix_ownership(log_dir, script_dir)
        log_file_handle = open(full_log_path, 'w', encoding='utf-8')
        fix_ownership(full_log_path, script_dir)
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
        log_queue.append(f"Wrapper Execution Error: {e!s}")
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

    # --- Repeat-failure suppression (opt-in via --repeat-after / --recovery) ---
    alert_note = ""
    state_update = None
    state_path = os.path.join(script_dir, "state", f"{safe_job_name}.json")
    tracking = args.mode in ("fail", "all") and (args.repeat_after > 0 or args.recovery)
    if tracking:
        base_send = should_send
        try:
            state = load_state(state_path)
            if exit_code != 0:
                fingerprint = failure_fingerprint(exit_code, timed_out_flag["value"], "\n".join(log_queue))
                should_send, alert_note, state_update = decide_failure_alert(
                    state, fingerprint, args.repeat_after, time.time())
            else:
                if args.recovery and state.get("last_alert"):
                    alert_note = (f"Recovered after {state.get('failures', 1)} failed run(s) "
                                  f"since {_fmt_ts(state.get('first_failure', time.time()))}")
                    should_send = True
                clear_state(state_path)
        except Exception as e:
            # A damaged state file must never swallow an alert or change the exit code:
            # fall back to the normal per-mode behaviour and rebuild the state next run.
            print(f"[cron-guard] WARNING: repeat tracking failed ({e!s}); alerting normally", file=sys.stderr)
            should_send, alert_note, state_update = base_send, "", None
            clear_state(state_path)

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
    delivered = True
    if should_send:
        final_log_path = full_log_path if keep_file else None
        delivered = send_telegram_alert(
            token, chat_id, args.job_name, exit_code, log_tail, duration_str, total_lines, final_log_path,
            timed_out=timed_out_flag["value"], creds_missing=creds_missing, timeout_limit=args.timeout,
            skip_missing_creds_log=True, note=alert_note
        )

    if state_update is not None:
        if should_send and not delivered:
            state_update["last_alert"] = 0   # never alerted -> never suppress the next run
        save_state(script_dir, state_path, state_update)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
