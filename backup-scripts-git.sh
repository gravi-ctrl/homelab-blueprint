#!/bin/bash

# ==============================================================================
# SCRIPT BACKUP WRAPPER
# ==============================================================================

# To capture the root crontab:
# 1. sudo visudo
# 2. Add this to the end of the file: gravi-ctrl ALL=(root) NOPASSWD: /usr/bin/crontab -l
# 3. Save.

# --- CONFIG ---
TARGET_DIR="/home/gravi-ctrl/scripts"
SNAPSHOT_DIR="$TARGET_DIR/run_once/system_configs"
MASTER_SCRIPT="$TARGET_DIR/git-auto-sync.sh"

# --- 1. SNAPSHOT SYSTEM CONFIGS ---
# Create folders
mkdir -p "$SNAPSHOT_DIR"
mkdir -p "$TARGET_DIR/run_once/dotfiles" # New folder for shell configs

# A. System Files
cp /etc/fstab "$SNAPSHOT_DIR/fstab.txt"
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt"

if sudo -n crontab -l > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    : 
else
    echo "Root crontab skipped" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# B. Installed Packages (The Shopping List)
apt-mark showmanual > "$SNAPSHOT_DIR/my_installed_apps.txt"

# C. Dotfiles (The "Home" Feel) --- NEW SECTION
# We copy them here so they are version controlled on GitHub
cp ~/.zshrc "$TARGET_DIR/run_once/dotfiles/zshrc"
cp ~/.p10k.zsh "$TARGET_DIR/run_once/dotfiles/p10k.zsh"
# If you have a custom nano config, grab that too
# cp ~/.nanorc "$TARGET_DIR/run_once/dotfiles/nanorc" 2>/dev/null

# --- 2. FORCE ADD SNAPSHOTS ---
cd "$TARGET_DIR" || exit
git add -f "run_once/system_configs/"
git add -f "run_once/dotfiles/"

# --- 3. HANDOFF TO MASTER SCRIPT ---
"$MASTER_SCRIPT" "$TARGET_DIR" "Scripts & System Configs"
