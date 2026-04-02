#!/bin/bash
# @DESCRIPTION: Snapshots cron/packages/dotfiles/hosts and syncs '~/scripts' & '/opt/stacks' to Git using `git-auto-sync.sh`
# @FREQUENCY: Daily 5am
# ==============================================================================
# SCRIPT BACKUP WRAPPER
# ==============================================================================

# --- CONFIG ---
# Load BACKUP_USER and TOOLS from .env
BACKUP_USER=$(grep '^BACKUP_USER=' "$(dirname "$(readlink -f "$0")")/.env" 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'")
TARGET_DIR="/home/$BACKUP_USER/scripts"
SNAPSHOT_DIR="$TARGET_DIR/run_once/system_configs"
MASTER_SCRIPT="$TARGET_DIR/git-auto-sync.py"
STACKS_DIR="/opt/stacks"

# Load TOOLS from .env
ENV_VAL=$(grep '^TOOLS=' "$(dirname "$(readlink -f "$0")")/.env" 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'")
TOOLS=(${ENV_VAL})

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "$TARGET_DIR/run_once/dotfiles"

# A. System Files & Raw Crons
cp /etc/hosts "$SNAPSHOT_DIR/hosts.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt"

if sudo -n crontab -l > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    :
else
    echo "Root crontab skipped" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# --- GENERATE HUMAN READABLE SCHEDULE ---
if [ -f "$TARGET_DIR/cron_translator.py" ]; then
    echo "Generating cron schedule..."
    python3 "$TARGET_DIR/cron_translator.py"
fi

# --- GENERATE SCRIPT INVENTORY ---
if [ -f "$TARGET_DIR/script_indexer.py" ]; then
    echo "Indexing Scripts..."
    python3 "$TARGET_DIR/script_indexer.py"
fi

# B. Installed Packages (subtract base OS)
apt-mark showmanual | sort > ~/scripts/run_once/system_configs/base-packages.txt

BASE_FILE="$SNAPSHOT_DIR/base-packages.txt"

if [ -f "$BASE_FILE" ]; then
    comm -23 \
      <(apt-mark showmanual | sort) \
      <(sort "$BASE_FILE") \
      > "$SNAPSHOT_DIR/my_installed_apps.txt"
else
    echo "⚠️  No baseline found — saving full list"
    apt-mark showmanual | sort > "$SNAPSHOT_DIR/my_installed_apps.txt"
fi

# C. Dotfiles
[ -f ~/.zshrc ] && cp ~/.zshrc "$TARGET_DIR/run_once/dotfiles/zshrc"
[ -f ~/.p10k.zsh ] && cp ~/.p10k.zsh "$TARGET_DIR/run_once/dotfiles/p10k.zsh"
[ -f /etc/nanorc ] && cp /etc/nanorc "$TARGET_DIR/run_once/dotfiles/nanorc"
[ -f ~/.hushlogin ] && cp ~/.hushlogin "$TARGET_DIR/run_once/dotfiles/hushlogin"

# Mirror specific .config folders
CONFIG_DEST="$TARGET_DIR/run_once/dotfiles/config"
mkdir -p "$CONFIG_DEST"

for tool in "${TOOLS[@]}"; do
    SOURCE_PATH="$HOME/.config/$tool"
    DEST_PATH="$CONFIG_DEST/$tool"

    if [ -d "$SOURCE_PATH" ]; then
        mkdir -p "$DEST_PATH"
        rsync -a --delete "$SOURCE_PATH/" "$DEST_PATH/"
    fi
done

# --- 2. HANDOFF TO MASTER SCRIPT ---
python3 "$MASTER_SCRIPT" "$TARGET_DIR" "Scripts & System Configs"

# --- 3. Sync /opt/stacks ---
python3 "$MASTER_SCRIPT" "$STACKS_DIR" "Server Stacks"
