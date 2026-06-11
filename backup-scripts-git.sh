#!/bin/bash
# @DESCRIPTION: Snapshots cron/packages/dotfiles/hosts/custom repos and syncs `~/scripts`, `~/ctrl_s_master` & `/opt/stacks` to Git using `git-auto-sync.sh`
# @FREQUENCY: Daily 5am
# @USES_ENV: STACKS_DIR, CTRL_DIR, SCRIPTS_DIR, TOOLS
# ==============================================================================
# SCRIPT BACKUP WRAPPER
# ==============================================================================

set -euo pipefail
IFS=$'\n\t'

[[ -f "$HOME/scripts/.env" ]] || { echo ".env does not exist at $HOME/scripts" >&2; exit 1; }
source "$HOME/scripts/.env"

IFS=' ' read -ra TOOLS_ARRAY <<< "${TOOLS:-}"
IFS=$'\n\t'

SNAPSHOT_DIR="${SCRIPTS_DIR}/run_once/system_configs"
MASTER_SCRIPT="${SCRIPTS_DIR}/git-auto-sync.py"

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "${SCRIPTS_DIR}/run_once/dotfiles"

# A. System Files & Raw Crons
cp /etc/hosts "$SNAPSHOT_DIR/hosts.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt" || true

if sudo -n crontab -l > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    :
else
    echo "Root crontab skipped" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# --- GENERATE HUMAN READABLE SCHEDULE ---
if [ -f "${SCRIPTS_DIR}/cron_translator.py" ]; then
    echo "Generating cron schedule..."
    "${SCRIPTS_DIR}/cron_translator.py"
fi

# --- GENERATE SCRIPT INVENTORY ---
if [ -f "${SCRIPTS_DIR}/script_indexer.py" ]; then
    echo "Indexing Scripts..."
    "${SCRIPTS_DIR}/script_indexer.py"
fi

# B. Installed Packages
apt-mark showmanual > "$SNAPSHOT_DIR/my_installed_apps.txt"

# C. Python (PIP) Packages
"$HOME/.venv/bin/pip" list --not-required --disable-pip-version-check 2>/dev/null \
    | awk 'NR>2 {print $1}' \
    | grep -viE "^(pip|setuptools|wheel|distribute)$" > "$SNAPSHOT_DIR/my_pip_packages.txt" || true

# D. APT Repositories (PPAs)
grep -rhoPe 'ppa\.launchpad(content)?\.net/\K[^/ ]+/[^/ ]+' /etc/apt/sources.list.d/ 2>/dev/null \
    | sort -u | sed 's/^/ppa:/' > "$SNAPSHOT_DIR/my_repos.txt" || true

# E. Dotfiles
[ -f ~/.zshrc ] && cp ~/.zshrc "${SCRIPTS_DIR}/run_once/dotfiles/zshrc"
[ -f ~/.p10k.zsh ] && cp ~/.p10k.zsh "${SCRIPTS_DIR}/run_once/dotfiles/p10k.zsh"
[ -f /etc/nanorc ] && cp /etc/nanorc "${SCRIPTS_DIR}/run_once/dotfiles/nanorc"
[ -f ~/.hushlogin ] && cp ~/.hushlogin "${SCRIPTS_DIR}/run_once/dotfiles/hushlogin"

# Mirror specific .config folders
CONFIG_DEST="${SCRIPTS_DIR}/run_once/dotfiles/config"
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
"$MASTER_SCRIPT" "${SCRIPTS_DIR}" "Scripts & System Configs"

# --- 3. Sync ctrl_s_master ---
"$MASTER_SCRIPT" "${CTRL_DIR}" "Security Master Update"

# --- 4. Sync /opt/stacks ---
"$MASTER_SCRIPT" "${STACKS_DIR}" "Server Stacks"

echo "✅ Backup process completed successfully."
