#!/bin/bash

# @DESCRIPTION: Auto-configures containers once they are manually started
# @FREQUENCY: Run Once
# @USES_ENV: TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID, N8N_WEBHOOK_UUID, WATCHER_TASKS

[[ -f /opt/scripts/.env ]] || { echo ".env not found at /opt/scripts" >&2; exit 1; }
source /opt/scripts/.env

STATE_FILE="/opt/scripts/.ghost_watcher_state"
touch "$STATE_FILE"

# ── Framework Helpers ─────────────────────────────────────────────────────────
is_running() { docker container inspect -f '{{.State.Status}}' "$1" 2>/dev/null | grep -q "running"; }
is_done()    { grep -q "^$1$" "$STATE_FILE" 2>/dev/null; }
mark_done()  { echo "$1" >> "$STATE_FILE"; }

send_telegram() {
    curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$1" > /dev/null
}

# ── Check Functions ───────────────────────────────────────────────────────────
# Define check_<name>() with the container name here to add custom readiness logic for a task.
# If no check_<name>() exists, the engine falls back to default_check().

default_check() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

check_nextcloud() {
    docker exec "$1" test -f /var/www/html/lib/versioncheck.php 2>/dev/null
}

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  TASK PAYLOADS
# ══════════════════════════════════════════════════════════════════════════════
# Create task_<name>() functions here.
# The active list of tasks is controlled by WATCHER_TASKS in /opt/scripts/.env

# 🔹 TASK: NEXTCLOUD
task_nextcloud() {
    sudo -u "$(stat -c '%U' /opt/scripts/.)" /opt/scripts/run_once/nextcloud_post-restore_fix.sh
    /opt/scripts/nextcloud-dynamic-watch.sh

    send_telegram "🔧 setup.sh's Post-Restore Watcher: Nextcloud
━━━━━━━━━━━━━━━
✅ Nextcloud post-restore and dynamic-watch scripts have been executed

🔍 Verify External storage:
Ensure 'assets' is listed in Administration settings > External storage.
(Requires 'External storage support' app)
If missing, manually re-add:
- Folder name: assets
- Restrict to: User
- External storage: Local
- Storage configuration: /mnt/external_files"
}

# 🔹 TASK: TAILSCALE
task_tailscale() {
    docker exec tailscaled tailscale serve reset

    local funnel_msg=""
    if [ -n "$N8N_WEBHOOK_UUID" ]; then
        docker exec tailscaled tailscale funnel --bg --https=443 --set-path="/webhook/${N8N_WEBHOOK_UUID}" "http://127.0.0.1:5678/webhook/${N8N_WEBHOOK_UUID}"
        docker exec tailscaled tailscale funnel --bg --https=443 --set-path="/webhook-test/${N8N_WEBHOOK_UUID}" "http://127.0.0.1:5678/webhook-test/${N8N_WEBHOOK_UUID}"
        funnel_msg+="✅ n8n webhooks configured"
    else
        funnel_msg+="❌ n8n webhooks skipped — N8N_WEBHOOK_UUID missing in .env\n"
    fi

    send_telegram "🔧 setup.sh's Post-Restore Watcher: Tailscale
━━━━━━━━━━━━━━━
${funnel_msg}

⚠️ If Tailscale connection fails, regenerate the auth key:
1. Go to https://login.tailscale.com/admin/settings/keys
2. Click 'Generate auth key'
3. Tick: Reusable + Tags → select a tag
4. Update TS_AUTHKEY in /opt/stacks/tailscale/.env

ℹ️ funnel = public internet, serve = tailnet-only:
docker exec tailscaled tailscale funnel --bg --https=443 \"http://127.0.0.1:PORT\"
docker exec tailscaled tailscale serve --bg --https=443 \"http://127.0.0.1:PORT\"
(non-443 ports need :PORT suffix in the .ts.net URL)"
}

# 🔹 TASK: NGINX PROXY MANAGER (NPM)
task_npm() {
    send_telegram "🔧 setup.sh's Post-Restore Watcher: NPM
━━━━━━━━━━━━━━━
ℹ️ NPM container is now running!

If you need to initialize a new CA and regenerate your local certificates:
1. Run: 'cert init' and 'cert regen'
2. Don't forget to restart Nginx right after:
   'docker restart npm'"
}

# ════════════════════════
# 🚀 MAIN WATCHER ENGINE
# ════════════════════════
run_check() {
    local task="$1" container="$2"
    local fn="check_${task}"
    if declare -f "$fn" > /dev/null; then
        "$fn" "$container"
    else
        default_check "$container"
    fi
}

mapfile -t TASK_LINES <<< "$WATCHER_TASKS"

while true; do
    ALL_DONE=true
    HAS_VALID_TASKS=false

    for line in "${TASK_LINES[@]}"; do
        [[ -z "${line// /}" ]] && continue
        [[ "$line" == \#* ]]   && continue

        HAS_VALID_TASKS=true
        IFS="|" read -r task container <<< "$line"

        is_done "$task" && continue

        ALL_DONE=false

        if is_running "$container" && run_check "$task" "$container"; then
            if declare -f "task_${task}" > /dev/null; then
                "task_${task}"
                mark_done "$task"
            fi
        fi
    done

    if [ "$ALL_DONE" = true ] || [ "$HAS_VALID_TASKS" = false ]; then
        systemctl disable container-watcher.service
        rm /etc/systemd/system/container-watcher.service
        systemctl daemon-reload
        rm "$STATE_FILE"
        break
    fi

    sleep 10
done
