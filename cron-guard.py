#!/usr/bin/env python3

# @DESCRIPTION: Executes commands on Linux/Windows with Telegram alerts (fail/success/all) and fallback logging to stderr and failed_alerts.log on delivery failure.
# @FREQUENCY: Varies
# @USES_ENV: TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID

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

def load_dotenv(filepath):
    if not os.path.isfile(filepath): return
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            if '=' in line:
                key, val = line.split('=', 1)
                os.environ[key.strip()] = val.strip().strip('"\'')

def send_telegram_alert(token, chat_id, job_name, exit_code, log_tail, duration):  # noqa: log_tail now used in fallback too
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    if exit_code == 0:
        status_header = "✅ <b>TASK SUCCESSFUL</b>"
        status_label = "Success"
    else:
        status_header = "🚨 <b>TASK FAILURE</b>"
        status_label = "Failed"

    text = (
        f"{status_header}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📂 <b>Job:</b> {html.escape(job_name)}\n"
        f"📊 <b>Status:</b> {status_label} (Code {exit_code})\n"
        f"⏱️ <b>Duration:</b> {duration}\n"
        f"⏰ <b>Finished:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━\n"
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

    # 1) stderr — captured by syslog/journald on Linux, Event Viewer on Windows
    print(alert_msg, file=sys.stderr)

    # 2) failed_alerts.log — sits next to cron-guard.py for manual inspection
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        fallback_log = os.path.join(script_dir, 'failed_alerts.log')
        with open(fallback_log, 'a', encoding='utf-8') as f:
            f.write(
                f"{timestamp} | job={job_name!r} | exit={exit_code} | duration={duration} | error={last_error}\n"
                f"--- last logs ---\n"
                f"{log_tail}\n"
                f"-----------------\n"
            )
    except Exception as log_err:
        # If even the file write fails, at least stderr already has it
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

    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(script_dir, '.env'))

    token = os.environ.get("TELEGRAM_DANTE_BOT_TOKEN", "YOUR_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_ID")

    # Capture start time
    start_time = time.time()
    log_queue = collections.deque(maxlen=15) # Increased to 15 lines

    child_env = os.environ.copy()
    venv_bin = os.path.expanduser(
        os.path.join("~", ".venv", "Scripts" if os.name == "nt" else "bin")
    )
    if os.path.isdir(venv_bin) and venv_bin not in child_env.get("PATH", ""):
        child_env["PATH"] = venv_bin + os.pathsep + child_env.get("PATH", "")
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONUNBUFFERED"] = "1"

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

        # Stream output in real-time
        for line in iter(process.stdout.readline, ''):
            clean_line = line.rstrip('\n')
            print(clean_line)
            log_queue.append(clean_line)

        exit_code = process.wait()
    except Exception as e:
        exit_code = 1
        log_queue.append(f"Wrapper Execution Error: {str(e)}")

    duration_seconds = int(time.time() - start_time)
    duration_str = str(datetime.timedelta(seconds=duration_seconds))

    # Logic to determine if we should send the alert
    should_send = False
    if args.mode == "all":
        should_send = True
    elif args.mode == "fail" and exit_code != 0:
        should_send = True
    elif args.mode == "success" and exit_code == 0:
        should_send = True

    if should_send:
        log_tail = "\n".join(log_queue) or "No output."
        send_telegram_alert(token, chat_id, args.job_name, exit_code, log_tail, duration_str)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
