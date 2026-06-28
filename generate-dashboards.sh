#!/bin/bash
# @DESCRIPTION: Runs both dashboard generators and safely commits the output to their respective pages branches.
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
# @CRON: User
# @USES_ENV: STACKS_DIR

set -e

[[ -f "/opt/rabbit-hole/.env" ]] || { echo ".env does not exist at /opt/rabbit-hole" >&2; exit 1; }
set -a
source "/opt/rabbit-hole/.env"
set +a

echo "🔄 Starting Dashboard Generation Pipeline..."

# Temporary paths to prevent branch-switching collisions
HOMELAB_TEMP="/tmp/homelab_index.html"
DOCKER_TEMP="/tmp/docker_index.html"

# ==========================================
# 1. Generate Dashboards
# ==========================================

# Generate Homelab Dashboard
echo "🖥️ Generating Homelab Dashboard..."
cd "/opt/rabbit-hole"
python3 homelab_dash.py

if [ -f "index.html" ]; then
    mv index.html "$HOMELAB_TEMP"
else
    echo "❌ Error: index.html was not found!"
    exit 1
fi

# Generate Docker Dashboard
echo "🐳 Generating Docker Dashboard..."
if [ -f "/opt/rabbit-hole/docker-dash/docker_dash.py" ]; then
    python3 "/opt/rabbit-hole/docker-dash/docker_dash.py" --env "/opt/rabbit-hole/docker-dash/.env" --out "$DOCKER_TEMP"
else
    echo "❌ Error: docker_dash.py not found at /opt/rabbit-hole/docker-dash/docker_dash.py"
    exit 1
fi

# ==========================================
# 2. Global Cleanup Mechanism
# ==========================================

# Global variables to track exactly what needs cleaning if a crash occurs
CURRENT_REPO=""
CURRENT_WORKTREE=""

cleanup_worktree() {
    # Only clean up if the variables are populated (meaning a deployment was active)
    if [[ -n "$CURRENT_REPO" && -n "$CURRENT_WORKTREE" ]]; then
        echo "🧹 Cleaning up temporary worktree..."
        cd "$CURRENT_REPO" 2>/dev/null || true
        rm -rf "$CURRENT_WORKTREE"
        git -C "$CURRENT_REPO" worktree prune 2>/dev/null || true
        CURRENT_REPO=""
        CURRENT_WORKTREE=""
    fi
}

# Trap EXIT (catches set -e crashes and normal script ends), INT (Ctrl+C), and TERM (kill commands)
trap cleanup_worktree EXIT INT TERM

# ==========================================
# 3. Deployment Function
# ==========================================

deploy_page_local() {
    # Assign globals so the trap knows what we are working on
    CURRENT_REPO="$1"
    local temp_html="$2"
    CURRENT_WORKTREE="/tmp/worktree_$(basename "$CURRENT_REPO")"

    if [ ! -d "$CURRENT_REPO" ]; then
        echo "⚠️ Warning: Directory $CURRENT_REPO does not exist. Skipping."

        # Reset globals since we are skipping
        CURRENT_REPO=""
        CURRENT_WORKTREE=""
        return 0
    fi

    echo "📁 Processing deployment for $CURRENT_REPO..."

    # Forcibly clear any stale worktree from a previous failed run
    git -C "$CURRENT_REPO" worktree remove "$CURRENT_WORKTREE" --force 2>/dev/null || true
    git -C "$CURRENT_REPO" worktree prune 2>/dev/null || true
    rm -rf "$CURRENT_WORKTREE"

    # Check out the 'pages' branch to the temp directory
    # (Creating it if it doesn't exist yet)
    if ! git -C "$CURRENT_REPO" show-ref --quiet refs/heads/pages; then
        echo "   (Initializing pages branch)"
        git -C "$CURRENT_REPO" branch pages
    fi

    # Check out the branch to the temporary worktree
    git -C "$CURRENT_REPO" worktree add "$CURRENT_WORKTREE" pages

    # Fetch any dashboard updates made by other machines!
    git -C "$CURRENT_WORKTREE" pull --rebase origin pages >/dev/null 2>&1 || true

    # Copy our generated HTML to the temporary worktree directory
    cp "$temp_html" "$CURRENT_WORKTREE/index.html"

    # Step into the temp folder to stage, diff, and commit
    cd "$CURRENT_WORKTREE"
    git add index.html

    if git diff --cached --quiet -I "[Gg]enerated "; then
        echo "   (Only timestamp changed in $CURRENT_REPO - skipping commit)"
    else
        git commit -m "Auto-update dashboard [skip ci]"
    fi

    # Clean up manually upon normal success
    cleanup_worktree
}

# ==========================================
# 4. Execute Deployments
# ==========================================

deploy_page_local "/opt/rabbit-hole" "$HOMELAB_TEMP"
deploy_page_local "$STACKS_DIR" "$DOCKER_TEMP"

echo "✅ Dashboard generation complete! Local 'pages' branches have been updated."
echo "   Your syncing script will handle pushing these branches to your remotes."
