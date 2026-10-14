#!/bin/bash
# @DESCRIPTION: Snapshots POSIX ownership and permissions of Git-tracked files to `.git-perms`, or applies them during bootstrap
# @FREQUENCY: Daily 5am (triggered by `backup-scripts-git.sh` & `bootstrap.sh`)
# @USES_ENV:
# @CRON: user
# ==============================================================================
# GIT PERMISSIONS PRESERVATION ENGINE
# ==============================================================================

set -euo pipefail
umask 0022

ACTION="${1:-}"
TARGET_DIR="${2:-.}"

if [ -z "$ACTION" ] || [[ ! "$ACTION" =~ ^(save|apply)$ ]]; then
    echo "Usage: $(basename "$0") <save|apply> [target_directory]" >&2
    exit 1
fi

if [ ! -d "$TARGET_DIR" ]; then
    echo "❌ Error: Target directory '$TARGET_DIR' does not exist." >&2
    exit 1
fi

cd "$TARGET_DIR"

if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    echo "❌ Error: '$TARGET_DIR' is not inside a Git repository." >&2
    exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
PERMS_FILE="$REPO_ROOT/.git-perms"

case "$ACTION" in
    save)
        # Determine the primary user of the repository root
        PRIMARY_USER=$(stat -c '%U' "$REPO_ROOT")
        PRIMARY_UID=$(stat -c '%u' "$REPO_ROOT")
        PRIMARY_GID=$(stat -c '%g' "$REPO_ROOT")

        TMP_PERMS=$(mktemp)

        # Capture all tracked files and their parent directories
        {
            git ls-files
            git ls-files | while IFS= read -r f; do dirname "$f"; done | sort -u
        } | while IFS= read -r path; do
            [ "$path" = "." ] && continue
            [ -e "$path" ] || [ -L "$path" ] || continue

            mode=$(stat -c '%a' "$path")
            owner=$(stat -c '%U' "$path")
            group=$(stat -c '%G' "$path")
            u_id=$(stat -c '%u' "$path")
            g_id=$(stat -c '%g' "$path")

            # Abstract the primary user so permissions remain portable across fresh installs/different UIDs
            if [ "$owner" = "$PRIMARY_USER" ] || [ "$u_id" -eq "$PRIMARY_UID" ]; then
                owner="@USER"
            fi
            if [ "$group" = "$PRIMARY_USER" ] || [ "$g_id" -eq "$PRIMARY_GID" ]; then
                group="@GROUP"
            fi

            printf "%s\t%s:%s\t%s\n" "$mode" "$owner" "$group" "$path"
        done | sort -k3 > "$TMP_PERMS"

        # Only overwrite if contents actually changed to avoid unnecessary git diffs
        if [ -f "$PERMS_FILE" ] && cmp -s "$TMP_PERMS" "$PERMS_FILE"; then
            rm -f "$TMP_PERMS"
            echo "  ✓ Permissions unchanged in $(basename "$REPO_ROOT")"
        else
            mv "$TMP_PERMS" "$PERMS_FILE"
            echo "  ✓ Saved permissions for $(wc -l < "$PERMS_FILE") paths to $(basename "$REPO_ROOT")/.git-perms"
        fi
        ;;

    apply)
        if [ ! -f "$PERMS_FILE" ]; then
            echo "  — No .git-perms found in $(basename "$REPO_ROOT"), skipping."
            exit 0
        fi

        # If run via sudo, get the actual user who invoked sudo, not root
        CURRENT_USER="${SUDO_USER:-$(id -un)}"
        CURRENT_GROUP="$(id -gn "$CURRENT_USER" 2>/dev/null || id -gn)"

        # Read tab-delimited records: <mode> <ownership> <path>
        while IFS=$'\t' read -r mode ownership path; do
            [ -z "$path" ] && continue
            [ -e "$path" ] || [ -L "$path" ] || continue

            # Resolve abstract placeholder to current execution user
            ownership="${ownership//@USER/$CURRENT_USER}"
            ownership="${ownership//@GROUP/$CURRENT_GROUP}"

            # Preserve symlinks safely (only chown the link itself, never chmod symlinks)
            sudo chown -h "$ownership" "$path" 2>/dev/null || true
            if [ ! -L "$path" ]; then
                sudo chmod "$mode" "$path" 2>/dev/null || true
            fi
        done < "$PERMS_FILE"

        echo "  ✓ Restored permissions from $(basename "$REPO_ROOT")/.git-perms"
        ;;
esac

exit 0
