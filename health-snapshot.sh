#!/bin/bash
# @DESCRIPTION: Sends an on-demand health snapshot of the server to Telegram
# @FREQUENCY: On Demand

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/.env"
source "$SCRIPT_DIR/bot-telegram/.env"

# ── Uptime ────────────────────────────────────────────────────
UPTIME=$(uptime -p | sed 's/up //')

# ── Load ──────────────────────────────────────────────────────
LOAD=$(uptime | awk -F'load average:' '{print $2}' | awk '{print $1}' | tr -d ',')

# ── CPU Temp ──────────────────────────────────────────────────
CPU_TEMP=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | \
    awk 'BEGIN{max=0} {if($1>max)max=$1} END{printf "%.0f°C", max/1000}')
[[ -z "$CPU_TEMP" ]] && CPU_TEMP="N/A"

# ── Disk ──────────────────────────────────────────────────────
DISK_USED=$(df -h /data | awk 'NR==2 {print $5}')
DISK_FREE=$(df -h /data | awk 'NR==2 {print $4}')

# ── Containers ────────────────────────────────────────────────
RUNNING=$(docker ps --format '{{.Status}}' | grep -c "^Up")
UNHEALTHY=$(docker ps --format '{{.Status}}' | grep -c "unhealthy" || true)

if [ "$UNHEALTHY" -gt 0 ]; then
    UNHEALTHY_NAMES=$(docker ps --format '{{.Names}} {{.Status}}' \
        | grep "unhealthy" | awk '{print $1}' | tr '\n' ' ' | sed 's/ $//')
    CONTAINER_LINE="🐳 ${RUNNING} running · ⚠️ unhealthy: ${UNHEALTHY_NAMES}"
else
    CONTAINER_LINE="🐳 ${RUNNING} running · ✅ all healthy"
fi

# ── Last Backup ───────────────────────────────────────────────
LATEST_BACKUP=$(ls -t "${BACKUP_DIR}"/docker-stacks-*.tar.zst.age 2>/dev/null | head -1)

if [ -n "$LATEST_BACKUP" ]; then
    BACKUP_DATE=$(stat -c %y "$LATEST_BACKUP" | cut -d' ' -f1)
    BACKUP_SIZE=$(du -sh "$LATEST_BACKUP" | cut -f1)
    BACKUP_AGE_DAYS=$(( ( $(date +%s) - $(stat -c %Y "$LATEST_BACKUP") ) / 86400 ))
    if [ "$BACKUP_AGE_DAYS" -le 8 ]; then
        BACKUP_LINE="🔒 Backup: ${BACKUP_DATE} ✅ · ${BACKUP_SIZE}"
    else
        BACKUP_LINE="🔒 Backup: ${BACKUP_DATE} ⚠️ ${BACKUP_AGE_DAYS}d ago · ${BACKUP_SIZE}"
    fi
else
    BACKUP_LINE="🔒 Backup: ❌ not found"
fi

# ── Timestamp ─────────────────────────────────────────────────
TIMESTAMP=$(date '+%a %b %d %H:%M')

# ── Assemble & Send ───────────────────────────────────────────
MESSAGE="🖥️ <b>homeserver</b> — ${TIMESTAMP}
━━━━━━━━━━━━━━━━━━━━━
⏱️ Uptime: ${UPTIME}
🌡️ CPU: ${CPU_TEMP} · Load: ${LOAD}
💾 /data: ${DISK_USED} used · ${DISK_FREE} free
${CONTAINER_LINE}
${BACKUP_LINE}
━━━━━━━━━━━━━━━━━━━━━"

curl -fsS "https://api.telegram.org/bot${VERGIL_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${ALLOWED_USER_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${MESSAGE}" \
    > /dev/null

echo "✅ Health snapshot sent."
