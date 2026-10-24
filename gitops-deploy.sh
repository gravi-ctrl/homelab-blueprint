#!/bin/bash
# @DESCRIPTION: Monitors /opt/stacks for remote Github changes done by Renovate and auto-deploys updated compose stacks.
# @FREQUENCY: Every 15 minutes
# @USES_ENV: STACKS_DIR, TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user
# @CAPABILITIES:
#   • Auto‑syncs only main branch, blocking feature branches to keep production stable.
#   • Detects merge conflicts, pauses deployment, and alerts admins via Telegram to prevent broken stacks.
#   • Redeploys only stacks with running containers, preserving intentionally stopped services.
#   • Uses non‑blocking lock to avoid race conditions during concurrent runs.
#   • Pulls images sequentially with retries, ensuring zero‑downtime updates even on transient registry failures.
#   • Tracks failed stacks in a git‑local file so one bad stack never blocks others and is retried automatically.

set -euo pipefail

[[ -f "/opt/rabbit-hole/.env" ]] || { echo ".env does not exist at /opt/rabbit-hole" >&2; exit 1; }
source "/opt/rabbit-hole/.env"

cd "${STACKS_DIR}"
PAUSE_FILE="/tmp/gitops_paused"
# Stacks whose last deploy failed. Lives inside .git so it is never committed or synced.
RETRY_FILE="${STACKS_DIR}/.git/gitops_retry"

# Prevent stepping on git-auto-sync.py or dashboard generation.
# Non-blocking: if busy, skip this run entirely.
exec 200>"${STACKS_DIR}/.git/sync.lock"
flock -n 200 || { echo "⏭️  Stacks repo busy (locked by another process), skipping this cycle."; exit 0; }

# Best effort: pull one image at a time to avoid containerd content-store races.
export COMPOSE_PARALLEL_LIMIT=1

send_telegram() {
    if [ -n "${TELEGRAM_DANTE_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=$1" > /dev/null || true
    fi
}

# Image pulls occasionally fail on transient registry/containerd errors; retry before giving up.
pull_with_retry() {
    local attempt
    for attempt in 1 2 3; do
        if docker compose pull -q; then
            return 0
        fi
        echo "⚠️  Pull attempt ${attempt}/3 failed." >&2
        if [ "$attempt" -lt 3 ]; then
            sleep 20
        fi
    done
    return 1
}

# Verify the server is on 'main' before touching live containers
CURRENT_BRANCH=$(git symbolic-ref --short -q HEAD || echo "detached")

if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "⏭️  Stacks repo is on branch '${CURRENT_BRANCH}' (expected 'main'). Skipping GitOps deploy."
    exit 0
fi

git fetch -q origin main

# Count how many remote commits we are missing
BEHIND=$(git rev-list HEAD..origin/main | wc -l)
CHANGED_DIRS=""
NEW_FAILED=""
PREV_FAILED=""

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

    rm -f "$PAUSE_FILE"
fi

# Add stacks that failed to deploy on an earlier run (git is already up to date for them,
# so they would never show up in the diff again).
if [ -s "$RETRY_FILE" ]; then
    PREV_FAILED=$(sort -u "$RETRY_FILE")
    echo "🔁 GitOps: Retrying previously failed stack(s)."
    CHANGED_DIRS=$(printf '%s\n%s\n' "$CHANGED_DIRS" "$PREV_FAILED" | sed '/^$/d' | sort -u)
fi

# Redeploy active stacks
if [ -n "$CHANGED_DIRS" ]; then
    REDEPLOYED_STACKS=""
    FAILED_DIRS=""

    for dir in $CHANGED_DIRS; do
        if [ -d "$dir" ] && [ -f "$dir/compose.yml" ]; then

            # Check if the stack has active running containers
            if [ -n "$(cd "$dir" && docker compose ps --services --status=running 2>/dev/null)" ]; then
                echo "🚀 GitOps: Redeploying active stack: $dir"
                # A failure here must not abort the loop: the remaining stacks still get deployed.
                if (cd "$dir" && pull_with_retry && docker compose up -d); then
                    REDEPLOYED_STACKS+="- ${dir}"$'\n'
                else
                    echo "❌ GitOps: Deploy failed for stack: $dir (old containers left running)"
                    FAILED_DIRS+="${dir}"$'\n'
                fi
            else
                echo "⏭️  GitOps: Skipping stopped stack: $dir (Files updated, but container left offline)"
            fi

        fi
    done

    NEW_FAILED=$(printf '%s' "$FAILED_DIRS" | sed '/^$/d' | sort -u)
    if [ -n "$NEW_FAILED" ]; then
        printf '%s\n' "$NEW_FAILED" > "$RETRY_FILE"
    else
        rm -f "$RETRY_FILE"
    fi

    if [ -n "$REDEPLOYED_STACKS" ]; then
        send_telegram "🔄 GitOps Sync: Successful
━━━━━━━━━━━━━━━
The following stacks were successfully updated and restarted:
$REDEPLOYED_STACKS"
    fi
fi

# Clear old pause file if the repository is now fully in sync
if [ "$BEHIND" -eq 0 ] && [ -f "$PAUSE_FILE" ]; then
    echo "🧹 GitOps: Repository is in sync. Clearing old pause flag."
    rm -f "$PAUSE_FILE"
fi

if [ -n "$NEW_FAILED" ]; then
    echo "🚨 GitOps: Deploy failed for: $(printf '%s' "$NEW_FAILED" | tr '\n' ' ')- will retry next cycle."
    # Exit non-zero only when the failure set is new, so cron-guard alerts once instead of every 15 minutes.
    if [ "$NEW_FAILED" != "$PREV_FAILED" ]; then
        exit 1
    fi
    exit 0
fi

if [ "$BEHIND" -gt 0 ] || [ -n "$PREV_FAILED" ]; then
    echo "✅ GitOps: Sync complete."
fi
