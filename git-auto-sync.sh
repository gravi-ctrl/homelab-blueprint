#!/bin/bash

# ==============================================================================
# GENERIC GIT AUTO-SYNC SCRIPT
# Usage: ./git-auto-sync.sh "/path/to/repo" "Commit Label"
# Example: ./git-auto-sync.sh "/opt/stacks" "Server Configs"
# ==============================================================================

TARGET_DIR="$1"
LABEL="${2:-Auto-Sync}" # Default to "Auto-Sync" if no label provided

# 1. Safety Check: Ensure a path was provided
if [ -z "$TARGET_DIR" ]; then
    echo "❌ Error: No directory path provided."
    echo "Usage: $0 /path/to/repo [Label]"
    exit 1
fi

# 2. Navigate to the folder
# If this fails, the script exits immediately (preventing accidental commits elsewhere)
cd "$TARGET_DIR" || { echo "❌ Error: Could not cd to $TARGET_DIR"; exit 1; }

# Download updates from GitHub before doing anything else.
# If there is a conflict (same line edited in both places), this might fail, and will require a human fix.
git pull origin main --no-edit

# 3. Stage all changes
git add .

# 4. Commit ONLY if there are changes
# We use a clean date format: YYYY-MM-DD HH:MM:SS
if ! git diff-index --quiet HEAD --; then
    git commit -m "$LABEL: $(date '+%Y-%m-%d %H:%M:%S')"
else
    # Optional: Print message for manual runs, but exit successfully
    echo "Everything up-to-date."
    # We do not exit here, because we still want to attempt a push 
    # (in case a previous push failed but commit succeeded)
fi

# 5. Push changes
# Try a standard push first
if git push 2>/dev/null; then
    : # Success, do nothing
else
    # If standard push fails, it might be a new branch needing upstream
    echo "⚠️ Standard push failed. Attempting to set upstream..."
    
    # "origin HEAD" automatically pushes the current branch name to origin
    if git push -u origin HEAD; then
        echo "✅ Upstream set and pushed successfully."
    else
        echo "❌ Push Failed."
        exit 1 # Triggers tg-alert
    fi
fi
