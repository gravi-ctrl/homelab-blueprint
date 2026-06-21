#!/usr/bin/env python3
# @DESCRIPTION: Runs scripts and commands directly on server with logs
# @FREQUENCY: On Demand (triggered by `setup.sh`)

import os
import sys
import subprocess
import io
import shlex
import html
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── Path & Config Setup ───────────────────────────────────────────────────────
SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_DIR  = SCRIPT_PATH.parent
SERVICE_NAME = "tg-vergil"
SERVICE_FILE = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

# Ensure .env is loaded from the exact directory this script lives in
load_dotenv(SCRIPT_DIR / '.env')

# Validate essential configuration immediately (aborts both run and install if missing)
TOKEN = os.getenv('TELEGRAM_VERGIL_BOT_TOKEN')
RAW_ALLOWED_ID = os.getenv('TELEGRAM_CHAT_ID')

if not TOKEN or not RAW_ALLOWED_ID:
    print("❌ Error: Missing TELEGRAM_VERGIL_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
    print("   Please configure your .env file before installing or running the bot.")
    sys.exit(1)

try:
    ALLOWED_ID = int(RAW_ALLOWED_ID)
except ValueError:
    print(f"❌ Error: TELEGRAM_CHAT_ID '{RAW_ALLOWED_ID}' in .env is not a valid integer.")
    sys.exit(1)

COMMAND_MAP = {k.replace('CMD_', '').lower(): v for k, v in os.environ.items() if k.startswith('CMD_')}

# ── Bot Logic ─────────────────────────────────────────────────────────────────
async def execute_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_ID:
        return

    trigger = update.message.text.split()[0][1:].lower()
    shell_command = COMMAND_MAP.get(trigger)

    if not shell_command:
        await update.message.reply_text("❌ Command configuration not found.")
        return

    status_msg = await update.message.reply_text(f"⏳ Running: {trigger}...")

    try:
        custom_env = os.environ.copy()

        venv_bin = os.path.join(sys.prefix, 'bin')
        current_path = custom_env.get('PATH', '')
        custom_env['PATH'] = f"{venv_bin}:{current_path}" if current_path else venv_bin

        result = subprocess.run(
            shlex.split(shell_command),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=custom_env  # <--- Pass the modified environment to the script
        )

        output = (result.stdout + result.stderr).strip() or "Success (No Output)"

        target_key = f"MUTE_{trigger}".upper()
        mute_on_success = any(
            k.upper() == target_key and v.strip().lower() == "true"
            for k, v in os.environ.items()
        )

        if mute_on_success and result.returncode == 0:
            await status_msg.edit_text(
                f"✅ <b>{trigger}</b> completed successfully!", 
                parse_mode=ParseMode.HTML
            )
            return

        if len(output) > 4000:
            file_obj = io.BytesIO(output.encode('utf-8'))
            file_obj.name = f"{trigger}_log.txt"
            await update.message.reply_document(
                document=file_obj,
                caption=f"✅ Output for <b>{trigger}</b> (Log too long for text)",
                parse_mode=ParseMode.HTML
            )
            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=status_msg.message_id
            )
        else:
            escaped_output = html.escape(output)
            await status_msg.edit_text(f"<pre>{escaped_output}</pre>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")


# ── Execution & Install Logic ─────────────────────────────────────────────────
if __name__ == '__main__':

    # 1. Trigger installer ONLY if explicitly requested via `--install`
    if "--install" in sys.argv:
        if SERVICE_FILE.exists():
            print(f"Service {SERVICE_NAME} is already installed.")
            print(f"To reinstall, delete {SERVICE_FILE} first.")
            sys.exit(0)

        print(f"Installing {SERVICE_NAME} service...")
        service_user = SCRIPT_PATH.owner()

        service_content = f"""[Unit]
Description=Telegram Remote Command Bot (Vergil)
After=network.target

[Service]
User={service_user}
WorkingDirectory={SCRIPT_DIR}
ExecStart=/opt/venv/bin/python3 {SCRIPT_PATH} --running-as-service
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
        subprocess.run(
            ["sudo", "tee", str(SERVICE_FILE)],
            input=service_content,
            text=True,
            check=True,
            stdout=subprocess.DEVNULL
        )
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
        subprocess.run(["sudo", "systemctl", "enable", "--now", f"{SERVICE_NAME}.service"], check=True)
        print(f"✅ Service installed and started as user: {service_user}")
        print(f"   Verify: sudo journalctl -u {SERVICE_NAME}.service -f")
        sys.exit(0)

    # 2. Otherwise, run the bot (foreground or background)
    app = ApplicationBuilder().token(TOKEN).build()

    for cmd_trigger in COMMAND_MAP:
        app.add_handler(CommandHandler(cmd_trigger, execute_script))
        print(f"Registered command: /{cmd_trigger}")

    if "--running-as-service" in sys.argv:
        print("Bot is active (Running as systemd service).")
    else:
        print("Bot is active (Running in foreground. Press Ctrl+C to stop).")

    app.run_polling()
