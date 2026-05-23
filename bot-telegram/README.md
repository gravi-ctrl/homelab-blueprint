# Telegram Remote Command Bot

A Python bot that runs server commands via Telegram. Commands are defined entirely in `.env` — no code edits needed to add or remove them.

## How It Works

1. On startup, the bot reads every `CMD_`-prefixed variable from `.env`
2. Each one becomes a Telegram `/command` (e.g. `CMD_backup` → `/backup`)
3. When triggered, it runs the shell command and replies with the output
4. If output exceeds 4000 characters, it sends a `.txt` log file instead
5. Only `ALLOWED_USER_ID` can execute commands — all others are silently ignored

## Installation

### 1. Install Dependencies
```bash
pip3 install python-telegram-bot python-dotenv --break-system-packages
```

### 2. Configure
```bash
cp .env.example .env
nano .env
```

Add commands using the `CMD_` prefix:
```ini
VERGIL_BOT_TOKEN=your-token
ALLOWED_USER_ID=your-telegram-id

CMD_backup="bash ~/scripts/backup.sh"
CMD_ping="ping -c 3 google.com"
CMD_health="bash ~/scripts/health-snapshot.sh"
```

### 3. Install & Start
```bash
python3 bot.py
```

The script self-installs as a systemd service (`tg-vergil`) on first run, enables it, and exits. From that point it runs automatically on boot.

You should see:
```
Installing tg-vergil service...
✅ Service installed and started.
   Verify: sudo journalctl -u tg-vergil.service -f
```

## Adding New Commands

1. Edit `.env` and add a new `CMD_` line
2. Restart: `sudo systemctl restart tg-vergil`

That's it — the new `/command` is live.

## Managing the Service

```bash
# Status
sudo systemctl status tg-vergil

# Logs
sudo journalctl -u tg-vergil -f

# Restart after .env changes
sudo systemctl restart tg-vergil
```
