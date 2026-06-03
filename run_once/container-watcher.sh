#!/bin/bash

# @DESCRIPTION: Auto-configures containers once they are manually started
# @FREQUENCY: Run Once

source /opt/scripts/.env
STATE_FILE="/opt/scripts/.ghost_watcher_state"
touch "$STATE_FILE"

# ── 1. Framework Helpers ──────────────────────────────────────────────────────
is_running() { docker container inspect -f '{{.State.Status}}' "$1" 2>/dev/null | grep -q "running"; }
is_done()    { grep -q "^$1$" "$STATE_FILE" 2>/dev/null; }
mark_done()  { echo "$1" >> "$STATE_FILE"; }

send_telegram() {
    curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$1" > /dev/null
}

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️ TASK PAYLOADS
# ══════════════════════════════════════════════════════════════════════════════
# Create your task_<name> functions here.
# The active list of tasks is controlled by WATCHER_TASKS in /opt/scripts/.env

task_nextcloud() {
    # Scripts
    sudo -u "$(stat -c '%U' /opt/scripts/.)" /opt/scripts/run_once/nextcloud_post-restore_fix.sh
    /opt/scripts/nextcloud-dynamic-watch.sh

    # Message
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

task_tailscale() {
    docker exec tailscaled tailscale serve reset

    if [ -n "$N8N_WEBHOOK_UUID" ]; then
        docker exec tailscaled tailscale funnel --bg --https=443 --set-path="/webhook/${N8N_WEBHOOK_UUID}" "http://127.0.0.1:5678/webhook/${N8N_WEBHOOK_UUID}"
        docker exec tailscaled tailscale funnel --bg --https=443 --set-path="/webhook-test/${N8N_WEBHOOK_UUID}" "http://127.0.0.1:5678/webhook-test/${N8N_WEBHOOK_UUID}"
        MSG_TEXT="✅ Tailscale Funnel configured!
🛡️ n8n webhooks successfully secured via path-based routing."
    else
        MSG_TEXT="❌ Tailscale Funnel skipped!
⚠️ WARNING: N8N_WEBHOOK_UUID is missing in /opt/scripts/.env!
For security reasons, n8n was not exposed. Please add the UUID to your .env to run the funnel."
    fi

    send_telegram "🔧 setup.sh's Post-Restore Watcher: Tailscale
━━━━━━━━━━━━━━━
${MSG_TEXT}

⚠️ If Tailscale connection fails, regenerate the auth key:
1. Go to https://login.tailscale.com/admin/settings/keys
2. Click 'Generate auth key'
3. Tick: Reusable + Tags → select a tag
4. Update TS_AUTHKEY in /opt/stacks/tailscale/.env"
}

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
mapfile -t TASK_LINES <<< "$WATCHER_TASKS"

while true; do
    ALL_DONE=true
    HAS_VALID_TASKS=false

    for line in "${TASK_LINES[@]}"; do
        # Ignore empty lines or lines with only spaces
        [[ -z "${line// /}" ]] && continue
        # Ignore lines that start with a comment (#)
        [[ "$line" == \#* ]] && continue

        HAS_VALID_TASKS=true

        # Unpack the current line into variables
        IFS="|" read -r task container custom_check <<< "$line"

        # 1. Skip if already completed
        if is_done "$task"; then
            continue
        fi

        ALL_DONE=false

        # 2. Check if container is running
        if is_running "$container"; then
            # 3. If there is a custom check, evaluate it
            if [ -z "$custom_check" ] || eval "$custom_check"; then
                # 4. Check if the payload function exists, run it, and mark done
                if declare -f "task_${task}" > /dev/null; then
                    "task_${task}"
                    mark_done "$task"
                fi
            fi
        fi
    done

    # ── Self-Destruct Sequence ─────────────────────────────────────────────
    if [ "$ALL_DONE" = true ] || [ "$HAS_VALID_TASKS" = false ]; then
        systemctl disable container-watcher.service
        rm /etc/systemd/system/container-watcher.service
        systemctl daemon-reload
        rm "$STATE_FILE"
        break
    fi

    sleep 10
done