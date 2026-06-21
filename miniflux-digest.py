#!/usr/bin/env python3
# @DESCRIPTION: Fetches new Miniflux entries since the last run and sends a digest via Telegram
# @FREQUENCY: Daily 7am
# @USES_ENV: MINIFLUX_API_URL, MINIFLUX_API_KEY, TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

MINIFLUX_API_URL = os.environ.get("MINIFLUX_API_URL", "").rstrip("/")
MINIFLUX_API_KEY = os.environ.get("MINIFLUX_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_DANTE_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = SCRIPT_DIR / ".miniflux_digest_state"
FETCH_LIMIT = 100          # how many recent entries to pull per run
TELEGRAM_SAFE_LIMIT = 3500  # stay safely under Telegram's 4096-char hard cap


def fail(msg):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(1)


if not MINIFLUX_API_URL or not MINIFLUX_API_KEY:
    fail("MINIFLUX_API_URL or MINIFLUX_API_KEY missing from /opt/ctrl/.env")
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    fail("TELEGRAM_DANTE_BOT_TOKEN or TELEGRAM_CHAT_ID missing from /opt/ctrl/.env")


def miniflux_get(path):
    req = urllib.request.Request(
        f"{MINIFLUX_API_URL}{path}",
        headers={"X-Auth-Token": MINIFLUX_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        fail(f"Could not reach Miniflux API at {MINIFLUX_API_URL}: {e}")


def send_telegram(text):
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.URLError as e:
        fail(f"Telegram send failed: {e}")


def load_last_seen_id():
    if STATE_FILE.exists():
        try:
            return int(STATE_FILE.read_text().strip())
        except ValueError:
            return 0
    return 0


def save_last_seen_id(entry_id):
    STATE_FILE.write_text(str(entry_id))


def chunk_text(full_text, limit):
    """Split on line boundaries so a single message never exceeds Telegram's limit."""
    chunks = []
    current = ""
    for line in full_text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def main():
    last_seen_id = load_last_seen_id()

    data = miniflux_get(f"/v1/entries?order=id&direction=desc&limit={FETCH_LIMIT}")
    entries = data.get("entries", [])

    new_entries = [e for e in entries if e["id"] > last_seen_id]
    if not new_entries:
        print("No new entries since last run.")
        return

    # Oldest-first within the digest, regardless of the desc order used to fetch
    new_entries.sort(key=lambda e: e["id"])

    by_feed = {}
    for e in new_entries:
        feed_title = e.get("feed", {}).get("title", "Unknown Feed")
        by_feed.setdefault(feed_title, []).append(e)

    lines = ["📰 Miniflux Digest", "━━━━━━━━━━━━━━━"]
    for feed_title, items in by_feed.items():
        lines.append(f"\n🔹 {feed_title}")
        for e in items:
            lines.append(f"• {e['title']}")
            lines.append(f"  {e['url']}")

    full_text = "\n".join(lines)

    for chunk in chunk_text(full_text, TELEGRAM_SAFE_LIMIT):
        send_telegram(chunk)

    save_last_seen_id(new_entries[-1]["id"])
    print(f"✅ Sent digest with {len(new_entries)} new entries across {len(by_feed)} feed(s).")


if __name__ == "__main__":
    main()
