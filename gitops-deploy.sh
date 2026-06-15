#!/bin/bash
# @DESCRIPTION: Monitors /opt/stacks for remote Codeberg changes done by Renovate and auto-deploys updated compose stacks.
# @FREQUENCY: Every 15 minutes
# @USES_ENV: STACKS_DIR, TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user

set -euo pipefail

[[ -f "/opt/scripts/.env" ]] || { echo ".env does not exist at /opt/scripts" >&2; exit 1; }
source "/opt/scripts/.env"

cd "${STACKS_DIR}"
PAUSE_FILE="/tmp/gitops_paused"

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

    # Attempt pull with autostash. Trigger lockout on failure.
    if ! git pull -q origin main --rebase --autostash; then
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
