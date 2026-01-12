#!/bin/bash

# ==============================================================================
# SCRIPT BACKUP WRAPPER
# ==============================================================================

# --- CONFIG ---
TARGET_DIR="/home/gravi-ctrl/scripts"
SNAPSHOT_DIR="$TARGET_DIR/run_once/system_configs"
MASTER_SCRIPT="$TARGET_DIR/git-auto-sync.sh"
TRANSLATOR_SCRIPT="$TARGET_DIR/cron_translator.py" # <--- NEW

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "$TARGET_DIR/run_once/dotfiles"

# A. System Files & Raw Crons
cp /etc/fstab "$SNAPSHOT_DIR/fstab.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt"

if sudo -n crontab -l > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    : 
else
    echo "Root crontab skipped" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# --- NEW: GENERATE HUMAN READABLE SCHEDULE ---
# This reads the txt files we just created and makes the Markdown dashboard
if [ -f "$TRANSLATOR_SCRIPT" ]; then
    python3 "$TRANSLATOR_SCRIPT"
fi
# ---------------------------------------------

# B. Installed Packages
apt-mark showmanual > "$SNAPSHOT_DIR/my_installed_apps.txt"

# C. Dotfiles
cp ~/.zshrc "$TARGET_DIR/run_once/dotfiles/zshrc"
cp ~/.p10k.zsh "$TARGET_DIR/run_once/dotfiles/p10k.zsh"

# --- 2. FORCE ADD SNAPSHOTS ---
cd "$TARGET_DIR" || exit
git add -f "run_once/system_configs/"
git add -f "run_once/dotfiles/"

# --- 3. HANDOFF TO MASTER SCRIPT ---
"$MASTER_SCRIPT" "$TARGET_DIR" "Scripts & System Configs"
