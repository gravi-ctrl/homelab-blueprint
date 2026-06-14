#!/bin/bash
# @DESCRIPTION: Monitors /opt/stacks for remote Codeberg changes done by Renovate and auto-deploys updated compose stacks.
# @FREQUENCY: Every 15 minutes
# @USES_ENV: STACKS_DIR, TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user

set -euo pipefail

[[ -f "/opt/scripts/.env" ]] || { echo ".env does not exist at /opt/scripts" >&2; exit 1; }
source "/opt/scripts/.env"

cd "${STACKS_DIR}"

# ── Telegram Helper ──
send_telegram() {
    if [ -n "${TELEGRAM_DANTE_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=$1" > /dev/null || true
    fi
}

# Enforce that we are strictly on the main branch
git checkout -q main

# Fetch remote changes silently
git fetch -q origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

# If they don't match, we have merged a PR!
if [ "$LOCAL" != "$REMOTE" ]; then
    echo "🔄 GitOps: Upstream changes detected on main branch."

    # Strictly triggers ONLY if the compose.yml file itself was modified
    CHANGED_DIRS=$(git diff --name-only HEAD origin/main | grep '/compose\.yml$' | cut -d/ -f1 | sort -u || true)

    # Pull the new docker-compose files
    git pull -q origin main

    # Re-deploy only the affected stacks that are currently active
    if [ -n "$CHANGED_DIRS" ]; then
        REDEPLOYED_STACKS=""

        for dir in $CHANGED_DIRS; do
            if [ -d "$dir" ] && [ -f "$dir/compose.yml" ]; then

                # Check if the stack has any active running containers
                if [ -n "$(cd "$dir" && docker compose ps --services --status=running 2>/dev/null)" ]; then
                    echo "🚀 GitOps: Redeploying active stack: $dir"
                    (cd "$dir" && docker compose pull -q && docker compose up -d)
                    REDEPLOYED_STACKS+="- ${dir}"$'\n'
                else
                    echo "⏭️  GitOps: Skipping stopped stack: $dir (Files updated, but container left offline)"
                fi

            fi
        done

        # ── Send the success notification! ──
        if [ -n "$REDEPLOYED_STACKS" ]; then
            send_telegram "🔄 GitOps Sync: Successful
━━━━━━━━━━━━━━━
The following stacks were successfully updated and restarted:
$REDEPLOYED_STACKS"
        fi
    fi
    echo "✅ GitOps: Sync complete."
fi
