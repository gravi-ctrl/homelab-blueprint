#!/bin/bash
# @DESCRIPTION: Monitors /opt/stacks for remote Codeberg changes done by Renovate and auto-deploys updated compose stacks.
# @FREQUENCY: Every 15 minutes
# @USES_ENV: STACKS_DIR, TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user
# @CAPABILITIES:
#   • Non-blocking flock prevents race conditions with concurrent sync processes.
#   • Auto-pauses and alerts admins via Telegram on merge conflicts.
#   • Redeploys only active stacks with modified compose files.
#   • Skips stopped stacks to preserve intentional offline states.
#   • Self-heals by clearing stale pause flags once repo syncs.
#   • Ensures zero-downtime updates via targeted image pulls.

set -euo pipefail

[[ -f "/opt/rabbit-hole/.env" ]] || { echo ".env does not exist at /opt/rabbit-hole" >&2; exit 1; }
source "/opt/rabbit-hole/.env"

cd "${STACKS_DIR}"
PAUSE_FILE="/tmp/gitops_paused"

# Prevent stepping on git-auto-sync.py or dashboard generation.
# Non-blocking: if busy, skip this run entirely.
exec 200>"${STACKS_DIR}/.git/sync.lock"
flock -n 200 || { echo "⏭️  Stacks repo busy (locked by another process), skipping this cycle."; exit 0; }

send_telegram() {
    if [ -n "${TELEGRAM_DANTE_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=$1" > /dev/null || true
    fi
}

# Enforce main branch tracking
git checkout -q main
git fetch -q origin main

# Count how many remote commits we are missing
BEHIND=$(git rev-list HEAD..origin/main | wc -l)

if [ "$BEHIND" -gt 0 ]; then
    echo "🔄 GitOps: Upstream changes detected on main branch."

    # Trigger only if a compose.yml file was modified
    CHANGED_DIRS=$(git diff --name-only HEAD origin/main | grep '/compose\.yml$' | cut -d/ -f1 | sort -u || true)

    # Attempt pull with autostash. Trigger lockout and restore clean state on failure.
    if ! git pull -q origin main --rebase --autostash; then
        git rebase --abort 2>/dev/null || true

        if [ ! -f "$PAUSE_FILE" ]; then
            send_telegram "🚨 GitOps Paused: Merge Conflict Detected
━━━━━━━━━━━━━━━
GitOps has been paused on the server to prevent spam. 

Please SSH into the machine and resolve the conflict manually. The script will automatically resume once resolved."
            touch "$PAUSE_FILE"
        fi
        exit 0
    fi

    # Redeploy active stacks
    if [ -n "$CHANGED_DIRS" ]; then
        REDEPLOYED_STACKS=""

        for dir in $CHANGED_DIRS; do
            if [ -d "$dir" ] && [ -f "$dir/compose.yml" ]; then

                # Check if the stack has active running containers
                if [ -n "$(cd "$dir" && docker compose ps --services --status=running 2>/dev/null)" ]; then
                    echo "🚀 GitOps: Redeploying active stack: $dir"
                    (cd "$dir" && docker compose pull -q && docker compose up -d)
                    REDEPLOYED_STACKS+="- ${dir}"$'\n'
                else
                    echo "⏭️  GitOps: Skipping stopped stack: $dir (Files updated, but container left offline)"
                fi

            fi
        done

        if [ -n "$REDEPLOYED_STACKS" ]; then
            send_telegram "🔄 GitOps Sync: Successful
━━━━━━━━━━━━━━━
The following stacks were successfully updated and restarted:
$REDEPLOYED_STACKS"
        fi
    fi

    rm -f "$PAUSE_FILE"
    echo "✅ GitOps: Sync complete."
fi

# Clear old pause file if the repository is now fully in sync
if [ "$BEHIND" -eq 0 ] && [ -f "$PAUSE_FILE" ]; then
    echo "🧹 GitOps: Repository is in sync. Clearing old pause flag."
    rm -f "$PAUSE_FILE"
fi
