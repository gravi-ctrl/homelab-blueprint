#!/usr/bin/env python3
"""
# @DESCRIPTION: Runs a command, captures output safely, and Telegrams on failure with logs
# @FREQUENCY: On Failure
"""
# USAGE: python cron-guard.py "NAME OF JOB" "command_to_run"

import sys
import os
import subprocess
import urllib.request
import urllib.parse
import datetime
import time
import html
import collections

def load_dotenv(filepath):
    if not os.path.isfile(filepath):
        return
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip().strip('"\'')
                if key not in os.environ:
                    os.environ[key] = val

def send_telegram_alert(token, chat_id, job_name, exit_code, log_tail):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    text = (
        f"🚨 <b>CRON FAILURE</b>\n\n"
        f"📂 <b>Job:</b> {html.escape(job_name)}\n"
        f"⏰ <b>Time:</b> {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🔢 <b>Exit Code:</b> {exit_code}\n\n"
        f"📜 <b>Log:</b>\n<pre>{html.escape(log_tail)}</pre>"
    )

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "text": text
    }).encode('utf-8')

    for _ in range(3):
        try:
            req = urllib.request.Request(url, data=data, method='POST')
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.getcode() == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False

def main():
    if len(sys.argv) < 3:
        print('USAGE: python cron-guard.py "NAME OF JOB" "command_to_run"')
        sys.exit(1)

    job_name = sys.argv[1]
    
    if os.name == 'nt':
        # Windows (cmd.exe) escaping
        command = subprocess.list2cmdline(sys.argv[2:])
    else:
        # Linux / macOS (/bin/sh) escaping
        import shlex
        command = shlex.join(sys.argv[2:])

    script_dir = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(script_dir, '.env'))

    token = os.environ.get("TELEGRAM_DANTE_BOT_TOKEN", "YOUR_HARDCODED_TOKEN_HERE")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_HARDCODED_CHAT_ID_HERE")
    skip_telegram = token.startswith("YOUR_HARDCODED")

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONUNBUFFERED"] = "1"

    log_queue = collections.deque(maxlen=10)
    
    try:
        with subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8', 
            errors='replace', 
            env=child_env     
        ) as process:
            for line in process.stdout:
                log_queue.append(line.rstrip('\n'))
                
        exit_code = process.wait()
    except Exception as e:
        exit_code = 1
        log_queue.append(f"Wrapper Execution Error: {str(e)}")

    if exit_code != 0:
        if skip_telegram:
            sys.exit(exit_code)

        log_tail = "\n".join(log_queue)
        if not log_tail.strip():
            log_tail = "No output."

        send_telegram_alert(token, chat_id, job_name, exit_code, log_tail)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()