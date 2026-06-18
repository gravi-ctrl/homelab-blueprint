#!/bin/bash
# @DESCRIPTION: Runs both dashboard generators and safely commits the output to their respective pages branches.
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
# @CRON: User
# @USES_ENV: STACKS_DIR

set -e

[[ -f "/opt/scripts/.env" ]] || { echo ".env does not exist at /opt/scripts" >&2; exit 1; }
set -a
source "/opt/scripts/.env"
set +a

echo "🔄 Starting Dashboard Generation Pipeline..."

# Temporary paths to prevent branch-switching collisions
HOMELAB_TEMP="/tmp/homelab_index.html"
DOCKER_TEMP="/tmp/docker_index.html"

# 1. Generate Homelab Dashboard
echo "🖥️ Generating Homelab Dashboard..."
cd "/opt/scripts"
python3 homelab_dash.py

if [ -f "index.html" ]; then
    mv index.html "$HOMELAB_TEMP"
else
    echo "❌ Error: index.html was not found!"
    exit 1
fi

# 2. Generate Docker Dashboard
echo "🐳 Generating Docker Dashboard..."
if [ -f "/opt/scripts/docker-dash/docker_dash.py" ]; then
    python3 "/opt/scripts/docker-dash/docker_dash.py" --env "/opt/scripts/docker-dash/.env" --out "$DOCKER_TEMP"
else
    echo "❌ Error: docker_dash.py not found at /opt/scripts/docker-dash/docker_dash.py"
    exit 1
fi

deploy_page_local() {
    local repo_dir="$1"
    local temp_html="$2"

    if [ ! -d "$repo_dir" ]; then
        echo "⚠️ Warning: Directory $repo_dir does not exist. Skipping."
        return 0
    fi

    echo "📁 Processing deployment for $repo_dir..."

    # 1. Define temporary paths
    local temp_worktree="/tmp/worktree_$(basename "$repo_dir")"

    # Cleanup
    cleanup_worktree() {
        cd "$repo_dir" 2>/dev/null || true
        rm -rf "$temp_worktree"
        git -C "$repo_dir" worktree prune 2>/dev/null || true
    }
    trap cleanup_worktree RETURN

    # 2. Forcibly clear any stale worktree from a previous failed run.
    git -C "$repo_dir" worktree remove "$temp_worktree" --force 2>/dev/null || true
    git -C "$repo_dir" worktree prune 2>/dev/null || true
    rm -rf "$temp_worktree"

    # 3. Check out the 'pages' branch to the temp directory
    # (Creating it if it doesn't exist yet)
    if ! git -C "$repo_dir" show-ref --quiet refs/heads/pages; then
        echo "   (Initializing pages branch)"
        git -C "$repo_dir" branch pages
    fi

    # Check out the branch normally without force-resetting it
    git -C "$repo_dir" worktree add "$temp_worktree" pages

    # Fetch any dashboard updates made by other machines!
    git -C "$temp_worktree" pull --rebase origin pages >/dev/null 2>&1 || true

    # 4. Copy our generated HTML to the temporary worktree directory
    cp "$temp_html" "$temp_worktree/index.html"

    # 5. Step into the temp folder to stage, diff, and commit
    cd "$temp_worktree"
    git add index.html

    if git diff --cached --quiet -I "Generated "; then
        echo "   (Only timestamp changed in $repo_dir - skipping commit)"
    else
        git commit -m "Auto-update dashboard [skip ci]"
    fi
}

# 3. Execute the deployments
deploy_page_local "/opt/scripts" "$HOMELAB_TEMP"
deploy_page_local "$STACKS_DIR" "$DOCKER_TEMP"

echo "✅ Dashboard generation complete! Local 'pages' branches have been updated."
echo "   Your syncing script will handle pushing these branches to your remotes."
