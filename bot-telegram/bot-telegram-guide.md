# Multi-Command Telegram Bot Guide

This guide sets up a Python bot that dynamically creates Telegram commands based on your `.env` file. You can add as many scripts as you want without editing the Python code.

## 1. Install Dependencies
```bash
sudo apt update
sudo apt install python3-pip
pip3 install python-telegram-bot python-dotenv
```

## 2. Setup Directory and Environment Variables

1. Create directory:
   ```bash
   mkdir -p /home/gravi-ctrl/scripts/bot-telegram
   ```

2. Create/Edit the `.env` file:
   ```bash
   nano /home/gravi-ctrl/script/bot-telegram/.env
   ```

3. **Configuration:**
   Use the prefix `CMD_` to define new commands.

   ```ini
   # Telegram Config
   VERGIL_BOT_TOKEN=TELEGRAM_TOKEN_HERE
   ALLOWED_USER_ID=CHAT_ID_HERE

   # --- COMMANDS ---
   # Syntax: CMD_commandName="shell command here"
   
   
   # Command: /copy
   CMD_copy="cd /path/to/file && cp file /path/to/destination"
   ```

## 3. Test the script
Run the script manually to verify it picks up your commands.
```bash
python3 /home/gravi-ctrl/scripts/bot-telegram/bot.py
```
*Output should say:*
> Registered command: /copy

## 4. System Service (Run Forever)

1. Create the service file:
   ```bash
   sudo nano /etc/systemd/system/tg-updater.service
   ```

2. Paste configuration:
   ```ini
   [Unit]
   Description=Telegram Multi-Command Bot
   After=network.target

   [Service]
   User=gravi-ctrl
   WorkingDirectory=/home/gravi-ctrl/scripts/bot-telegram
   ExecStart=/usr/bin/python3 /home/gravi-ctrl/scripts/bot-telegram/bot.py
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

3. Enable and Start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable tg-updater
   sudo systemctl restart tg-updater
   ```

## How to add more commands later?
1. Open `.env`: `nano /home/gravi-ctrl/scripts/bot-telegram/.env`
2. Add a new line: `CMD_whatever="echo hi"`
3. Restart the bot: `sudo systemctl restart tg-updater`
