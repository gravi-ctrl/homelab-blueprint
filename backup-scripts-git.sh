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
mkdir -p "$SNAPSHOT_DIR"

# Copy fstab (Save as .txt so it isn't ignored by *.bak rule)
cp /etc/fstab "$SNAPSHOT_DIR/fstab.txt"

# Copy User Crontab
crontab -l > "$SNAPSHOT_DIR/user_crontab.txt"

# This creates a list of all apt packages you installed
apt-mark showmanual > "$SNAPSHOT_DIR/installed_packages.txt"

# Copy Root Crontab (Only works if script is run with sudo or user has NOPASSWD)
# We add a check: if sudo fails, write a placeholder message.
if sudo -n crontab -l > "$SNAPSHOT_DIR/root_crontab.txt" 2>/dev/null; then
    : # Success
else
    echo "Root crontab skipped (Permission Denied)" > "$SNAPSHOT_DIR/root_crontab.txt"
fi

# --- 2. FORCE ADD SNAPSHOTS ---
# We force add this specific folder to ensure .gitignore doesn't hide them
cd "$TARGET_DIR" || exit
git add -f "run_once/system_configs/"

# --- 3. HANDOFF TO MASTER SCRIPT ---
"$MASTER_SCRIPT" "$TARGET_DIR" "Scripts & System Configs"
