#!/usr/bin/env python3

# @DESCRIPTION: Executes commands on Linux/Windows with Telegram alerts (fail/success/all) and fallback logging to stderr and failed_alerts.log on delivery failure.
# @FREQUENCY: Varies
# @USES_ENV: TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user, root

# optional: sudo ln -s cron-guard.py /usr/local/bin/cron-guard

# USAGE:
# python cron-guard.py --mode fail "My Backup" "bash backup.sh" (Only if it breaks)
# python cron-guard.py --mode all "Weekly Sync" "rsync -av ..." (Always notify)
# python cron-guard.py --mode success "Health Check" "curl ..." (Only notify if it works)

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

def load_dotenv(filepath):
    if not os.path.isfile(filepath): return
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            if '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip().strip('"\'')

def send_telegram_alert(token, chat_id, job_name, exit_code, log_tail, duration, total_lines, full_log_path):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    if exit_code == 0:
        status_header = "✅ <b>TASK SUCCESSFUL</b>"
        status_label = "Success"
    elif exit_code in (-15, 143, 130, -2): # Standard SIGTERM/SIGINT codes (Linux & Windows)
        status_header = "🛑 <b>TASK KILLED</b>"
        status_label = "Terminated by OS/User"
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
        f"📜 <b>Last Logs:</b>\n<pre>{html.escape(log_tail)}</pre>"
    )

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "text": text
    }).encode('utf-8')

    last_error = None
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

    try:
        script_dir = os.path.dirname(os.path.realpath(__file__))
        fallback_log = os.path.join(script_dir, 'failed_alerts.log')
        with open(fallback_log, 'a', encoding='utf-8') as f:
            f.write(
                f"{timestamp} | job={job_name!r} | exit={exit_code} | duration={duration} | error={last_error}\n"
                f"--- last logs ---\n{log_tail}\n-----------------\n"
            )
    except Exception as log_err:
        print(f"[cron-guard] WARNING: Could not write to failed_alerts.log: {log_err}", file=sys.stderr)

    return False

def main():
    parser = argparse.ArgumentParser(description="Run a command and notify via Telegram.")
    parser.add_argument("--mode", choices=["fail", "success", "all"], default="fail", 
                        help="When to send notification (default: fail)")
    parser.add_argument("job_name", help="Name of the job for the report")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="The command to execute")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

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

    start_time = time.time()
    log_queue = collections.deque(maxlen=15)
    total_lines = 0

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONUNBUFFERED"] = "1"

    process = None

    def forward_signal(signum, frame):
        if process and process.poll() is None:
            log_queue.append(f"⚠️ [cron-guard] Received Signal {signum}, killing child process...")
            try:
                process.terminate()
            except Exception:
                pass

    try:
        signal.signal(signal.SIGTERM, forward_signal)
        signal.signal(signal.SIGINT, forward_signal)
    except ValueError:
        pass 

    safe_job_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', args.job_name)
    log_dir = os.path.join(script_dir, "logs")
    full_log_path = os.path.join(log_dir, f"{safe_job_name}_latest.log")
    log_file_handle = None

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file_handle = open(full_log_path, 'w', encoding='utf-8')
    except Exception:
        full_log_path = None

    try:
        process = subprocess.Popen(
            full_cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding='utf-8',
            errors='replace',
            env=child_env
        )

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
        # Guaranteed closure of file stream to unlock it for Windows deletion
        if log_file_handle and not log_file_handle.closed:
            log_file_handle.close()

    duration_seconds = int(time.time() - start_time)
    duration_str = str(datetime.timedelta(seconds=duration_seconds))

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
        send_telegram_alert(token, chat_id, args.job_name, exit_code, log_tail, duration_str, total_lines, final_log_path)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
