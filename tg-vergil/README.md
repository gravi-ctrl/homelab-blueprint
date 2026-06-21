# Telegram Remote Command Bot - TG-Vergil

A Python bot that runs server commands via Telegram. Commands are defined entirely in `.env` — no code edits needed to add or remove them.

## How It Works

1. On startup, the bot reads every `CMD_`-prefixed variable from `.env`
2. Each one becomes a Telegram `/command` (e.g. `CMD_backup` → `/backup`)
3. When triggered, it runs the shell command and replies with the output
   * **Optional Muting:** If `MUTE_<COMMAND>="true"` is set in `.env`, the bot will only show a short success confirmation instead of dumping the raw logs. Useful for scripts that already handle their own notifications.
4. If output exceeds 4000 characters, it sends a `.txt` log file instead
5. Only `TELEGRAM_CHAT_ID` can execute commands — all others are silently ignored

## Installation

### 1. Install Dependencies
```bash
/opt/venv/bin/pip install python-telegram-bot python-dotenv
```

### 2. Configure
```bash
cp .env.example .env
nano .env
```

Add commands using the `CMD_` prefix:
```ini
TELEGRAM_VERGIL_BOT_TOKEN=your-token
TELEGRAM_CHAT_ID=your-telegram-id

CMD_backup="bash ~/scripts/backup.sh"
CMD_ping="ping -c 3 google.com"
MUTE_ping="true"
```

### 3. Install & Start
```bash
python3 vergil.py --install
```

The script self-installs as a systemd service (`tg-vergil`) on first run, enables it, and exits. From that point it runs automatically on boot.

You should see:
```
Installing tg-vergil service...
✅ Service installed and started.
   Verify: sudo journalctl -u tg-vergil.service -f
```

## Testing & Debugging

If you want to test new commands, view real-time console logs, or troubleshoot errors, you can easily run the bot in the foreground.

**Run the bot interactively:**
```bash
python3 vergil.py
```
*You will see the bot register your `.env` commands and confirm it is running in the foreground. Any errors or print statements will output directly to your terminal.*

## Adding New Commands

1. Edit `.env` and add a new `CMD_` line (and optional `MUTE_` line if you want to silence success logs)
2. Restart: `sudo systemctl restart tg-vergil`

That's it — the new `/command` is live.

## Managing the Service

```bash
# Status
sudo systemctl status tg-vergil

# Stop
sudo systemctl stop tg-vergil

# Start
sudo systemctl start tg-vergil

# Logs
sudo journalctl -u tg-vergil -f

# Restart after .env changes
sudo systemctl restart tg-vergil
```
