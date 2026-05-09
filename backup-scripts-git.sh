#!/bin/bash
# @DESCRIPTION: Snapshots cron/packages/dotfiles/hosts/custom repos and syncs `~/scripts`, `~/ctrl_s_master` & `/opt/stacks` to Git using `git-auto-sync.sh`
# @FREQUENCY: Daily 5am
# ==============================================================================
# SCRIPT BACKUP WRAPPER
# ==============================================================================

set -euo pipefail
IFS=$'\n\t'

# --- CONFIG ---
# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "Could not find .env in $SCRIPT_DIR/.env"
    exit 1
fi

IFS=' ' read -ra TOOLS_ARRAY <<< "${TOOLS:-}"
IFS=$'\n\t'

CTRL_S_DIR="/home/$BACKUP_USER/ctrl_s_master"
SNAPSHOT_DIR="$SCRIPT_DIR/run_once/system_configs"
MASTER_SCRIPT="$SCRIPT_DIR/git-auto-sync.py"
STACKS_DIR="/opt/stacks"

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "$SCRIPT_DIR/run_once/dotfiles"

# A. System Files & Raw Crons
cp /etc/hosts "$SNAPSHOT_DIR/hosts.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt" || true

if sudo -n crontab -l > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    :
else
    echo "Root crontab skipped" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# --- GENERATE HUMAN READABLE SCHEDULE ---
if [ -f "$SCRIPT_DIR/cron_translator.py" ]; then
    echo "Generating cron schedule..."
    "$SCRIPT_DIR/cron_translator.py"
fi

# --- GENERATE SCRIPT INVENTORY ---
if [ -f "$SCRIPT_DIR/script_indexer.py" ]; then
    echo "Indexing Scripts..."
    "$SCRIPT_DIR/script_indexer.py"
fi

# B. Installed Packages
apt-mark showmanual > "$SNAPSHOT_DIR/my_installed_apps.txt"

# C. APT Repositories (PPAs)
grep -rhoPe 'ppa\.launchpad(content)?\.net/\K[^/ ]+/[^/ ]+' /etc/apt/sources.list.d/ 2>/dev/null \
    | sort -u | sed 's/^/ppa:/' > "$SNAPSHOT_DIR/my_repos.txt" || true

# D. Dotfiles
[ -f ~/.zshrc ] && cp ~/.zshrc "$SCRIPT_DIR/run_once/dotfiles/zshrc"
[ -f ~/.p10k.zsh ] && cp ~/.p10k.zsh "$SCRIPT_DIR/run_once/dotfiles/p10k.zsh"
[ -f /etc/nanorc ] && cp /etc/nanorc "$SCRIPT_DIR/run_once/dotfiles/nanorc"
[ -f ~/.hushlogin ] && cp ~/.hushlogin "$SCRIPT_DIR/run_once/dotfiles/hushlogin"

# Mirror specific .config folders
CONFIG_DEST="$SCRIPT_DIR/run_once/dotfiles/config"
mkdir -p "$CONFIG_DEST"

for tool in "${TOOLS_ARRAY[@]}"; do
    SOURCE_PATH="$HOME/.config/$tool"
    DEST_PATH="$CONFIG_DEST/$tool"

    if [ -d "$SOURCE_PATH" ]; then
        mkdir -p "$DEST_PATH"
        rsync -a --delete "$SOURCE_PATH/" "$DEST_PATH/"
    fi
done

# --- 2. HANDOFF TO MASTER SCRIPT ---
"$MASTER_SCRIPT" "$SCRIPT_DIR" "Scripts & System Configs"

# --- 3. Sync ctrl_s_master ---
"$MASTER_SCRIPT" "$CTRL_S_DIR" "Security Master Update"

# --- 4. Sync /opt/stacks ---
"$MASTER_SCRIPT" "$STACKS_DIR" "Server Stacks"
