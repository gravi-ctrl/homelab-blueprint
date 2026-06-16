#!/bin/bash
# @DESCRIPTION: Runs both dashboard generators and safely commits the output to their respective pages branches.
# @FREQUENCY:   Daily 4:30am
# @CRON:        User
# @USES_ENV:    STACKS_DIR

set -e

# 1. Resolve scripts directory dynamically (follows symlinks to find the real git repo)
SCRIPTS_DIR="$(dirname "$(realpath "$0")")"

[[ -f "/opt/scripts/.env" ]] || { echo ".env does not exist at /opt/scripts" >&2; exit 1; }
source "/opt/scripts/.env"

echo "🔄 Starting Dashboard Generation Pipeline..."

# Temporary paths to prevent branch-switching collisions
HOMELAB_TEMP="/tmp/homelab_index.html"
DOCKER_TEMP="/tmp/docker_index.html"

# 3. Generate Homelab Dashboard
echo "🖥️ Generating Homelab Dashboard..."
cd "$SCRIPTS_DIR"
python3 homelab_dash.py

if [ -f "index.html" ]; then
    mv index.html "$HOMELAB_TEMP"
else
    echo "❌ Error: homelab_dashboard.html was not found!"
    exit 1
fi

# 4. Generate Docker Dashboard
echo "🐳 Generating Docker Dashboard..."
if [ -f "$SCRIPTS_DIR/docker-dash/docker_dash.py" ]; then
    python3 "$SCRIPTS_DIR/docker-dash/docker_dash.py" --out "$DOCKER_TEMP"
else
    echo "❌ Error: docker_dash.py not found at $SCRIPTS_DIR/docker-dash/docker_dash.py"
    exit 1
fi

# 5. Helper function to deploy a page to the local 'pages' branch of any git repository
deploy_page_local() {
    local repo_dir="$1"
    local temp_html="$2"

    if [ ! -d "$repo_dir" ]; then
        echo "⚠️ Warning: Directory $repo_dir does not exist. Skipping."
        return 0
    fi

    cd "$repo_dir"

    # Save the original branch so we can return to it safely
    local original_branch
    original_branch="$(git branch --show-current)"

    echo "📁 Deploying dashboard to $repo_dir (branch: pages)..."

    # Clean up any leftover index.html on the dev branch to prevent blocking checkout
    [ -f "index.html" ] && rm "index.html"

    # Switch to pages branch (or create it if it doesn't exist)
    git checkout pages || git checkout -b pages

    # Move the new dashboard in
    mv "$temp_html" index.html

    # Stage, commit locally, and ignore if no changes were made
    git add index.html
    git commit -m "Auto-update dashboard [skip ci]" || echo "   (No changes to commit in $repo_dir)"

    # Switch back to the original development branch
    git checkout "$original_branch"
}

# 6. Execute the deployments
deploy_page_local "$SCRIPTS_DIR" "$HOMELAB_TEMP"
deploy_page_local "$STACKS_DIR" "$DOCKER_TEMP"

echo "✅ Dashboard generation complete! Local 'pages' branches have been updated."
echo "   Your syncing script will handle pushing these branches to your remotes."
