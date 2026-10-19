#!/bin/bash
# @DESCRIPTION: Runs both dashboard generators and safely commits the output to their respective pages branches.
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh`)
# @CRON: User
# @USES_ENV: STACKS_DIR
# @CAPABILITIES:
#   • Deploys dashboards to isolated pages branches, keeping production code untouched.
#   • Automatically cleans stale worktrees on exit, preventing orphan temp directories.
#   • Suppresses repeated alerts with alarm_once flags, reducing noise until issues resolve.
#   • Aborts rebase conflicts, resets pages to origin, and re-arms alarms for quick recovery.
#   • Creates orphan pages branch if missing, ensuring dashboards never contaminate code branch.

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
# 3. Alarm-once helpers
# ==========================================
# A problem makes this script exit 1 (so cron-guard alarms) only the FIRST time. While the
# same problem persists, later runs log it but exit 0 (silent). The flag lives inside the
# repo's .git dir (survives reboots) and clears itself once the problem is gone.
alarm_flag() { echo "$(git -C "$1" rev-parse --absolute-git-dir)/dash-alarm-$2"; }

alarm_once() {   # $1=repo  $2=key  $3=message   -> returns 1 on first occurrence, 0 if already alarmed
    local flag; flag="$(alarm_flag "$1" "$2")"
    if [ -f "$flag" ]; then
        echo "🔕 Still blocked ($2) in $1 — already alarmed, staying silent until fixed."
        return 0
    fi
    : > "$flag"
    echo "❌ $3" >&2
    return 1
}
clear_alarm() { rm -f "$(alarm_flag "$1" "$2")"; }

# ==========================================
# 4. Deployment Function
# ==========================================
# NOTE: this function is called as `deploy_page_local ... || RC=1`, which DISABLES `set -e`
# inside it, so every step that can fail is checked explicitly.

deploy_page_local() {
    # Assign globals so the trap knows what we are working on
    CURRENT_REPO="$1"
    local repo="$1" temp_html="$2" rc=0
    CURRENT_WORKTREE="/tmp/worktree_$(basename "$CURRENT_REPO")"
    local wt="$CURRENT_WORKTREE"

    if [ ! -d "$repo" ]; then
        echo "⚠️ Warning: Directory $repo does not exist. Skipping."
        CURRENT_REPO=""; CURRENT_WORKTREE=""
        return 0
    fi

    echo "📁 Processing deployment for $repo..."

    # Forcibly clear any stale worktree from a previous failed run
    git -C "$repo" worktree remove "$wt" --force 2>/dev/null || true
    git -C "$repo" worktree prune 2>/dev/null || true
    rm -rf "$wt"

    # Attach (or create) the 'pages' branch in a temporary worktree.
    # A brand-new pages branch is NEVER a copy of the code branch: it starts from origin/pages if
    # that exists, otherwise as an empty orphan, so it can never carry (or delete) your stack files.
    local ok=1
    if git -C "$repo" show-ref --quiet refs/heads/pages; then
        git -C "$repo" worktree add -q "$wt" pages 2>/dev/null || ok=0
    elif git -C "$repo" fetch -q origin pages 2>/dev/null; then
        echo "   (Creating pages branch from origin/pages)"
        git -C "$repo" worktree add -q -b pages "$wt" FETCH_HEAD 2>/dev/null || ok=0
    else
        echo "   (Initializing empty orphan pages branch)"
        git -C "$repo" worktree add -q --orphan -b pages "$wt" 2>/dev/null || ok=0
    fi
    if [ "$ok" -eq 0 ]; then
        # Typically: 'pages' is checked out in the main working tree (e.g. a sync that got stuck there)
        alarm_once "$repo" worktree "Could not attach the 'pages' worktree in $repo (is 'pages' checked out in the main working tree? run: git -C $repo branch --show-current)"
        rc=$?; cleanup_worktree; return $rc
    fi
    clear_alarm "$repo" worktree

    # Fetch any dashboard updates made by other machines!
    if git -C "$wt" pull --rebase origin pages >/dev/null 2>&1; then
        clear_alarm "$repo" conflict
    else
        local gd; gd="$(git -C "$wt" rev-parse --absolute-git-dir)"
        if [ -d "$gd/rebase-merge" ] || [ -d "$gd/rebase-apply" ]; then
            # REAL conflict (same file, same place). Restore a clean state, alarm once.
            git -C "$wt" rebase --abort >/dev/null 2>&1 || true
            alarm_once "$repo" conflict "pages rebase conflict in $repo (our dashboard vs origin/pages). To resolve and re-arm: git -C $repo fetch origin pages && git -C $repo branch -f pages origin/pages   (discards the unpushed local dashboard commit; it regenerates next run)"
            rc=$?; cleanup_worktree; return $rc
        fi
        # Anything else (network blip, no remote pages yet): stay silent, exactly as before.
    fi

    # Copy our generated HTML to the temporary worktree directory
    if ! cp "$temp_html" "$wt/index.html"; then
        alarm_once "$repo" stage "Could not copy $temp_html into the pages worktree"
        rc=$?; cleanup_worktree; return $rc
    fi
    if ! git -C "$wt" add index.html 2>/dev/null; then
        alarm_once "$repo" stage "git add index.html failed in the pages worktree of $repo (is index.html ignored by a .gitignore on pages?)"
        rc=$?; cleanup_worktree; return $rc
    fi
    clear_alarm "$repo" stage

    if git -C "$wt" diff --cached --quiet -I "[Gg]enerated "; then
        echo "   (Only timestamp changed in $repo - skipping commit)"
    else
        if ! git -C "$wt" commit -q -m "Auto-update dashboard [skip ci]"; then
            alarm_once "$repo" commit "git commit failed in the pages worktree of $repo"
            rc=$?; cleanup_worktree; return $rc
        fi
        clear_alarm "$repo" commit
        echo "   (Committed dashboard update)"
    fi

    # Clean up manually upon normal success
    cleanup_worktree
    return 0
}

# ==========================================
# 5. Execute Deployments
# ==========================================
# Run BOTH repos even if the first one raises an alarm, then report.
RC=0
deploy_page_local "/opt/rabbit-hole" "$HOMELAB_TEMP" || RC=1
deploy_page_local "$STACKS_DIR" "$DOCKER_TEMP" || RC=1

if [ "$RC" -eq 0 ]; then
    echo "✅ Dashboard generation complete! Local 'pages' branches have been updated."
    echo "   Your syncing script will handle pushing these branches to your remotes."
fi
exit "$RC"
