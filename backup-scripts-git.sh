#!/bin/bash
# @DESCRIPTION: Snapshots cron/packages/dotfiles/hosts/repos/dashboards and syncs `~/scripts`, `~/ctrl_s_master` & `/opt/stacks` to Git using `git-auto-sync.sh`
# @FREQUENCY: Daily 5am
# @USES_ENV: STACKS_DIR, CTRL_DIR, TOOLS
# @CRON: user
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
MASTER_SCRIPT="/opt/rabbit-hole/git-auto-sync.py"

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "/opt/rabbit-hole/run_once/dotfiles"

# A. System Files & Raw Crons
cp /etc/hosts "$SNAPSHOT_DIR/hosts.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt" || true

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

echo "🚀 Syncing Repositories..."

# --- 2. HANDOFF TO MASTER SCRIPT ---
"$MASTER_SCRIPT" "/opt/rabbit-hole" "Scripts & System Configs"

# --- 3. Sync ctrl_s_master ---
"$MASTER_SCRIPT" "${CTRL_DIR}" "Security Master Update"

# --- 4. Sync /opt/stacks ---
"$MASTER_SCRIPT" "${STACKS_DIR}" "Server Stacks"

# --- GENERATE DASHBOARDS ---
if [ -f "/opt/rabbit-hole/generate-dashboards.sh" ]; then
    echo "📊 Generating Dashboards..."
    "/opt/rabbit-hole/generate-dashboards.sh"

    # --- 5. PUSH FRESH DASHBOARD COMMITS ---
    echo "🚀 Pushing updated dashboards..."
    git -C "/opt/rabbit-hole" push origin pages
    git -C "${STACKS_DIR}" push origin pages
fi

echo "✅ Backup process completed successfully."
