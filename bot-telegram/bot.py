#!/usr/bin/env python3

# @DESCRIPTION: Runs scripts and commands directly on server with logs (programmed in the .env file)
# @FREQUENCY: On Demand - Telegram

import os
import subprocess
import io
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv('BOT_TOKEN')
ALLOWED_ID = int(os.getenv('ALLOWED_USER_ID'))

COMMAND_MAP = {k.replace('CMD_', '').lower(): v for k, v in os.environ.items() if k.startswith('CMD_')}

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
        # Run command
        result = subprocess.run(
            shell_command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        output = (result.stdout + result.stderr).strip() or "Success (No Output)"

        if len(output) > 4000:
            file_obj = io.BytesIO(output.encode('utf-8'))
            file_obj.name = f"{trigger}_log.txt"
            await update.message.reply_document(
                document=file_obj,
                caption=f"✅ Output for <b>{trigger}</b> (Log too long for text)",
                parse_mode=ParseMode.HTML
            )
            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=status_msg.message_id)

        else:
            await status_msg.edit_text(f"<pre>{output}</pre>", parse_mode=ParseMode.HTML)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

if __name__ == '__main__':
    if not TOKEN or not ALLOWED_ID:
        print("Error: Missing BOT_TOKEN or ALLOWED_USER_ID in .env")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    for cmd_trigger in COMMAND_MAP:
        app.add_handler(CommandHandler(cmd_trigger, execute_script))
        print(f"Registered command: /{cmd_trigger}")

    print("Bot is active.")
    app.run_polling()
