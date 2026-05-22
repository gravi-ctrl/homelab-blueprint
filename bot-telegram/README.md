# Telegram Remote Command Bot

A Python bot that lets you run server commands via Telegram. Commands are defined entirely in `.env` — no code edits needed to add more.

## How It Works

1. On startup, the bot reads every `CMD_` prefixed variable from `.env`
2. Each one becomes a Telegram `/command` (e.g. `CMD_backup` → `/backup`)
3. When triggered, it runs the shell command, and replies with the output
4. If output exceeds 4000 characters, it sends a `.txt` log file instead
5. Only the `ALLOWED_USER_ID` can execute commands — all others are silently ignored

## Installation

### 1. Install Dependencies

```bash
sudo apt update && sudo apt install python3-pip
pip3 install python-telegram-bot python-dotenv
```

### 2. Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
nano .env
```

Add commands using the `CMD_` prefix:

```ini
CMD_backup="bash /home/user/scripts/backup.sh"
CMD_ping="ping -c 3 google.com"
```

### 3. Test

```bash
python3 bot.py
```

You should see:

```
Registered command: /backup
Registered command: /ping
Bot is active.
```

### 4. Create System Service

```bash
sudo nano /etc/systemd/system/tg-updater.service
```

```ini
[Unit]
Description=Telegram Remote Command Bot
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

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tg-updater
```

## Adding New Commands

1. Edit `.env` and add a new `CMD_` line
2. Restart: `sudo systemctl restart tg-updater`

That's it — the new `/command` is live.
