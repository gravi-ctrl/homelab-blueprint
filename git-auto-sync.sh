#!/bin/bash
# @DESCRIPTION: Master logic to push/pull Git repos
# @FREQUENCY: Varies
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

# 5. Push changes with Retry Logic
# We try up to 3 times to account for network blips
MAX_RETRIES=3
COUNT=0
SUCCESS=0

while [ $COUNT -lt $MAX_RETRIES ]; do
    # Try standard push
    if git push 2>/dev/null; then
        SUCCESS=1
        break
    fi

    # Check if the failure was just a missing upstream (First run only)
    if [ $COUNT -eq 0 ]; then
        if git push -u origin HEAD 2>/dev/null; then
            echo "✅ Upstream set and pushed successfully."
            SUCCESS=1
            break
        fi
    fi

    echo "⚠️ Push failed (Attempt $((COUNT+1))/$MAX_RETRIES). Retrying in 10s..."
    sleep 10
    COUNT=$((COUNT+1))
done

if [ $SUCCESS -eq 0 ]; then
    echo "❌ Push Failed after $MAX_RETRIES attempts."
    # Ensure we print the actual error on the final attempt so you can see it in logs
    git push
    exit 1
fi
