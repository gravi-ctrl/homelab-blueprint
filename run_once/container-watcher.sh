#!/bin/bash

# @DESCRIPTION: Auto-configures containers once they are manually started
# @FREQUENCY: Run Once
# @USES_ENV: TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID, N8N_WEBHOOK_UUID

source /opt/scripts/.env || { echo "❌ .env not found"; exit 1; }

STATE_FILE="/opt/scripts/.ghost_watcher_state"
POLL_INTERVAL=10
touch "$STATE_FILE"

# ── Helpers ───────────────────────────────────────────────────────────────────

is_running() { docker container inspect -f '{{.State.Status}}' "$1" 2>/dev/null | grep -q "running"; }
is_done()    { grep -q "^$1$"        "$STATE_FILE" 2>/dev/null; }
is_failed()  { grep -q "^FAILED:$1$" "$STATE_FILE" 2>/dev/null; }
mark_done()  { echo "$1"        >> "$STATE_FILE"; }
mark_failed(){ echo "FAILED:$1" >> "$STATE_FILE"; }

_tg() {
    curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${TELEGRAM_CHAT_ID}" \
        -d "parse_mode=HTML" \
        --data-urlencode "text=$1" > /dev/null
}
notify_ok()   { _tg "🔧 <b>Ghost Watcher</b> · $1"; }
notify_err()  { _tg "❌ <b>Ghost Watcher — FAILED</b> · $1"; }
notify_warn() { _tg "⚠️ <b>Ghost Watcher</b> · $1"; }

# ══════════════════════════════════════════════════════════════════════════════
# ⚙️  TASKS
# ══════════════════════════════════════════════════════════════════════════════
#
# To add a task, do these three things in order — all within this section:
#
#   ┌─ STEP 1 (optional) ───────────────────────────────────────────────────────
#   │ If "container is running" isn't enough, add a readiness function.
#   │ The watcher won't run the task until this returns 0.
#   │
#   │   my_ready() { docker exec myapp curl -fs http://localhost/health; }
#   └───────────────────────────────────────────────────────────────────────────
#
#   ┌─ STEP 2 (required) ───────────────────────────────────────────────────────
#   │ Add a line to the TASKS array. Order here = run order.
#   │
#   │   "my_task  CONTAINER=myapp"
#   │
#   │   With a readiness check:
#   │   "my_task  CONTAINER=myapp  READY=my_ready"
#   └───────────────────────────────────────────────────────────────────────────
#
#   ┌─ STEP 3 (required) ───────────────────────────────────────────────────────
#   │ Write the task function. Name must match what you used in STEP 2.
#   │ Use notify_ok / notify_warn / notify_err to send Telegram messages.
#   │
#   │   my_task() {
#   │       docker exec myapp some-setup-command
#   │       notify_ok "My app is configured. ✅"
#   │   }
#   └───────────────────────────────────────────────────────────────────────────
#
# ══════════════════════════════════════════════════════════════════════════════

# ── Readiness checks ──────────────────────────────────────────────────────────

nc_ready() {
    docker exec nextcloud php occ status 2>/dev/null | grep -q "installed: true"
}

# ── Task registry ─────────────────────────────────────────────────────────────
# Format: "task_name  CONTAINER=name  READY=fn"
# Order here = run order.

TASKS=(
    "nextcloud  CONTAINER=nextcloud  READY=nc_ready"
    "tailscale  CONTAINER=tailscaled"
    "npm        CONTAINER=npm"
)

# ── Task functions ────────────────────────────────────────────────────────────

# 🔹 TASK: NEXTCLOUD
nextcloud() {
    sudo -u "$(stat -c '%U' /opt/scripts/.)" /opt/scripts/run_once/nextcloud_post-restore_fix.sh
    /opt/scripts/nextcloud-dynamic-watch.sh

    notify_ok "Nextcloud configured.
━━━━━━━━━━━━━━━
✅ Post-restore and dynamic-watch scripts executed.

🔍 Verify External storage:
Ensure <b>assets</b> is listed under Administration → External storage.
If missing, re-add:
  Folder name: <code>assets</code> | Type: Local | Path: <code>/mnt/external_files</code>"
}

# 🔹 TASK: TAILSCALE
tailscale() {
    docker exec tailscaled tailscale serve reset

    if [ -n "$N8N_WEBHOOK_UUID" ]; then
        docker exec tailscaled tailscale funnel --bg --https=443 --set-path="/webhook/${N8N_WEBHOOK_UUID}" "http://127.0.0.1:5678/webhook/${N8N_WEBHOOK_UUID}"
        docker exec tailscaled tailscale funnel --bg --https=443 --set-path="/webhook-test/${N8N_WEBHOOK_UUID}" "http://127.0.0.1:5678/webhook-test/${N8N_WEBHOOK_UUID}"
        local webhook_status="✅ n8n webhooks configured"
    else
        local webhook_status="❌ n8n webhooks skipped — N8N_WEBHOOK_UUID missing"
    fi

    docker exec tailscaled tailscale funnel --bg --https=443 "http://127.0.0.1:80"

    notify_ok "Tailscale configured.
━━━━━━━━━━━━━━━
${webhook_status}
✅ Obsidian (ignis) configured

⚠️ If Tailscale fails, regenerate the auth key:
<a href='https://login.tailscale.com/admin/settings/keys'>tailscale.com → Tick: Reusable → Tags → Select a tag</a>
Then update <code>TS_AUTHKEY</code> in /opt/stacks/tailscale/.env"
}

# 🔹 TASK: NGINX PROXY MANAGER (NPM)
npm() {
    notify_ok "NPM is running.
━━━━━━━━━━━━━━━
ℹ️ To init a new CA and regenerate certs:
  1. <code>cert init</code> then <code>cert regen</code>
  2. <code>docker restart npm</code>"
}

# ══════════════════════════════════════════════════════════════════════════════
# 🚀 WATCHER ENGINE — Do not touch the below
# ══════════════════════════════════════════════════════════════════════════════

self_destruct() {
    systemctl disable container-watcher.service
    rm -f /etc/systemd/system/container-watcher.service
    systemctl daemon-reload
    rm -f "$STATE_FILE"
}

while true; do
    ALL_DONE=true

    for entry in "${TASKS[@]}"; do
        # First word is the task name, rest is key=value metadata
        read -r name rest <<< "$entry"
        declare -A meta=()
        for kv in $rest; do meta["${kv%%=*}"]="${kv#*=}"; done

        is_done   "$name" && continue
        is_failed "$name" && continue

        ALL_DONE=false

        # Container must be running
        is_running "${meta[CONTAINER]}" || continue

        # Optional readiness check — safe named-function call, no eval
        [[ -n "${meta[READY]:-}" ]] && { "${meta[READY]}" 2>/dev/null || continue; }

        # Run in a subshell so a crash can't corrupt watcher state
        if ( "$name" ); then
            mark_done "$name"
        else
            notify_err "Task <b>${name}</b> failed. Check logs on the server."
            mark_failed "$name"
        fi
    done

    [[ "$ALL_DONE" == true ]] && { self_destruct; break; }

    sleep "$POLL_INTERVAL"
done
