#!/bin/bash
# @DESCRIPTION: Snapshots cron/packages/dotfiles/hosts/repos/dashboards/permissions, pushes dashboards, dotfiles and system_configs
# @FREQUENCY: Daily 5am
# @USES_ENV: STACKS_DIR, CTRL_DIR, TOOLS
# @CRON: user
# @CAPABILITIES:
#   • Creates a full system snapshot enabling rapid rollback and compliance auditing.
#   • Backs up APT source lists and keyrings, preserving exact repository trust for reproducible builds.
#   • Mirrors user .config tool folders via rsync, keeping settings consistent across machines.
#   • Saves Git repository permissions before sync, preventing accidental exposure of sensitive files.
#   • Generates dashboards and pushes to a dedicated pages branch only when no alarm flags, reducing alert fatigue.
#   • Publishes snapshots to a separate branch, ensuring mainline stability while retaining daily state history.
#   • Reports uncommitted changes across key repos, flagging drift for proactive remediation.
#   • Skips dashboard pushes when alarm flags exist, preventing repeated failures and maintaining alarm‑once design.
# ==============================================================================
# SCRIPT BACKUP WRAPPER
# ==============================================================================

set -euo pipefail
umask 0022
IFS=$'\n\t'

[[ -f "/opt/rabbit-hole/.env" ]] || { echo ".env does not exist at /opt/rabbit-hole" >&2; exit 1; }
source "/opt/rabbit-hole/.env"

IFS=' ' read -ra TOOLS_ARRAY <<< "${TOOLS:-}"
IFS=$'\n\t'

SNAPSHOT_DIR="/opt/rabbit-hole/run_once/system_configs"

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "/opt/rabbit-hole/run_once/dotfiles"

# A. System Files & Raw Crons
cp /etc/hosts "$SNAPSHOT_DIR/hosts.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt" || true

# shellcheck disable=SC2024
if sudo read-root-crontab > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    :
else
    echo "Root crontab skipped" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# B. Installed Packages
apt-mark showmanual > "$SNAPSHOT_DIR/my_installed_apps.txt"

# C. Python (PIP) Packages
"/opt/venv/bin/pip" list --not-required --disable-pip-version-check 2>/dev/null \
    | awk 'NR>2 {print $1}' \
    | grep -viE "^(pip|setuptools|wheel|distribute)$" > "$SNAPSHOT_DIR/my_pip_packages.txt" || true

# D. APT Repositories — backup source files + keyrings as-is
REPOS_BACKUP_DIR="$SNAPSHOT_DIR/apt_sources"
mkdir -p "$REPOS_BACKUP_DIR/keyrings/usr_share"
mkdir -p "$REPOS_BACKUP_DIR/keyrings/etc_apt"

# Copy source list files
for f in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    [[ "$(basename "$f")" =~ ^(ubuntu|debian)\.sources$ ]] && continue
    cp "$f" "$REPOS_BACKUP_DIR/"
done

# Copy keyrings — preserve which directory they came from
for f in /usr/share/keyrings/*; do
    [[ ! "$(basename "$f")" =~ ^(debian|ubuntu)- ]] && [ -f "$f" ] && cp "$f" "$REPOS_BACKUP_DIR/keyrings/usr_share/"
done
for f in /etc/apt/keyrings/*; do
    [ -f "$f" ] && cp "$f" "$REPOS_BACKUP_DIR/keyrings/etc_apt/"
done

# Legacy PPA list for add-apt-repository
grep -rhoPe 'ppa\.launchpad(content)?\.net/\K[^/ ]+/[^/ ]+' \
    /etc/apt/sources.list.d/ 2>/dev/null \
    | sort -u | sed 's/^/ppa:/' > "$SNAPSHOT_DIR/my_repos.txt" || true

# E. Dotfiles
[ -f ~/.zshrc ] && cp ~/.zshrc "/opt/rabbit-hole/run_once/dotfiles/zshrc"
[ -f ~/.p10k.zsh ] && cp ~/.p10k.zsh "/opt/rabbit-hole/run_once/dotfiles/p10k.zsh"
[ -f /etc/nanorc ] && cp /etc/nanorc "/opt/rabbit-hole/run_once/dotfiles/nanorc"
[ -f ~/.hushlogin ] && cp ~/.hushlogin "/opt/rabbit-hole/run_once/dotfiles/hushlogin"

# Snapshot custom sshd drop-in configs
SSHD_DEST="$SNAPSHOT_DIR/sshd_config.d"
mkdir -p "$SSHD_DEST"
if compgen -G "/etc/ssh/sshd_config.d/*.conf" > /dev/null; then
    cp /etc/ssh/sshd_config.d/*.conf "$SSHD_DEST/"
fi

# Mirror specific .config folders
CONFIG_DEST="/opt/rabbit-hole/run_once/dotfiles/config"
mkdir -p "$CONFIG_DEST"

for tool in "${TOOLS_ARRAY[@]}"; do
    SOURCE_PATH="$HOME/.config/$tool"
    DEST_PATH="$CONFIG_DEST/$tool"

    if [ -d "$SOURCE_PATH" ]; then
        mkdir -p "$DEST_PATH"
        rsync -a --delete "$SOURCE_PATH/" "$DEST_PATH/"
    fi
done

# F. REPOSITORY FILE PERMISSIONS (.git-perms)
echo "🔒 Snapshotting Git repository permissions..."
if [ -f "/opt/rabbit-hole/git-perms.sh" ]; then
    "/opt/rabbit-hole/git-perms.sh" save "/opt/rabbit-hole"
    "/opt/rabbit-hole/git-perms.sh" save "${CTRL_DIR}"
    "/opt/rabbit-hole/git-perms.sh" save "${STACKS_DIR}"
fi

# G. SCRIPT CAPABILITIES
if [ -f "/opt/rabbit-hole/doc-injector.py" ]; then
    echo "🤖 Auditing script capabilities..."
    python3 "/opt/rabbit-hole/doc-injector.py" || true
fi

# --- GENERATE DASHBOARDS ---
if [ -f "/opt/rabbit-hole/generate-dashboards.sh" ]; then
    echo "📊 Generating Dashboards..."
    "/opt/rabbit-hole/generate-dashboards.sh"

    # --- 5. PUSH FRESH DASHBOARD COMMITS ---
    echo "🚀 Pushing updated dashboards..."
    for repo in "/opt/rabbit-hole" "${STACKS_DIR}"; do
        # generate-dashboards.sh leaves a dash-alarm-* flag in .git while a problem (e.g. a pages
        # conflict) is known and already alarmed. Pushing then would fail every day and defeat
        # the alarm-once design, so skip until the flag clears.
        if compgen -G "$(git -C "$repo" rev-parse --absolute-git-dir)/dash-alarm-*" > /dev/null; then
            echo "🔕 Skipping pages push for $repo (dashboard problem already alarmed)."
        else
            git -C "$repo" push origin pages
        fi
    done
fi

# --- PUBLISH SNAPSHOTS (to the 'snapshots' branch; never touches main) ---
publish_snapshots() {
    local repo="/opt/rabbit-hole" wt="/tmp/worktree_snapshots" p

    git -C "$repo" worktree remove "$wt" --force 2>/dev/null || true
    git -C "$repo" worktree prune
    rm -rf "$wt"

    if git -C "$repo" show-ref --quiet refs/heads/snapshots; then
        git -C "$repo" worktree add -q "$wt" snapshots
    elif git -C "$repo" fetch -q origin snapshots 2>/dev/null; then
        git -C "$repo" worktree add -q -b snapshots "$wt" FETCH_HEAD
    else
        git -C "$repo" worktree add -q --orphan -b snapshots "$wt"
    fi

    for p in run_once/system_configs run_once/dotfiles; do
        mkdir -p "$wt/$p"
        rsync -a --delete "$repo/$p/" "$wt/$p/"
    done

    printf '%s\n' \
    '/*' \
    '!/.gitignore' \
    '!/run_once/' \
    '/run_once/*' \
    '!/run_once/system_configs/' \
    '!/run_once/dotfiles/' \
    '.env' '**/.env' '*.pem' '*.key' > "$wt/.gitignore"

    git -C "$wt" add -A
    if git -C "$wt" diff --cached --quiet; then
        echo "   (No snapshot changes)"
    else
        git -C "$wt" commit -q -m "Snapshot $(date +%F)"
        git -C "$wt" push -q -u origin snapshots
    fi

    git -C "$repo" worktree remove "$wt" --force
}
publish_snapshots

# --- DRIFT REPORT (never commits) ---
DRIFT=""
for repo in "/opt/rabbit-hole" "${CTRL_DIR}" "${STACKS_DIR}"; do
    changes="$(git -C "$repo" status --porcelain -- . ':(exclude).git-perms')"
    if [ -n "$changes" ]; then
        DRIFT+="📂 $repo"$'\n'"$changes"$'\n'
    fi
done

STATE_FILE="/opt/rabbit-hole/.git/backup-drift.hash"
if [ -n "$DRIFT" ]; then
    echo "⚠️  Uncommitted changes:"
    echo "$DRIFT"
    # repeat once a week while drift presists
    new_hash="$(printf '%s%s' "$DRIFT" "$(date +%G-%V)" | sha256sum | cut -d' ' -f1)"
    if [ "$(cat "$STATE_FILE" 2>/dev/null || true)" != "$new_hash" ]; then
        echo "$new_hash" > "$STATE_FILE"
        exit 1    # cron-guard alerts once per distinct drift, not every morning
    fi
else
    rm -f "$STATE_FILE"
fi

echo "✅ Backup process completed successfully."
