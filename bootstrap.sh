#!/bin/bash
# @DESCRIPTION: Phase 1 Bootstrap: Decrypts & restores a Day-0 archive, fixes SSH permissions, removes cloud-init and re-links blueprint git repositories.
# @FREQUENCY: Run Once (Disaster Recovery)

set -euo pipefail

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================
GIT_HOST="codeberg.org"
GIT_HOST_FALLBACK="github.com"
GIT_USER="gravi-ctrl"
REPO_SCRIPTS="homelab-blueprint"
REPO_CTRL="ctrl-s-master"
REPO_STACKS="server-docker-backup"

DIR_SCRIPTS="$HOME/scripts"
DIR_CTRL="$HOME/ctrl_s_master"
DIR_STACKS="/opt/stacks"

AGE_KEYFILE="/root/.backup-key.txt"
# ==============================================================================

[[ $EUID -eq 0 ]] && { echo "ERROR: Don't run as root." >&2; exit 1; }

echo "======================================================="
echo " 🛡️  SERVER BOOTSTRAP: DEPLOYMENT MODE"
echo "======================================================="
echo "  1) Full Recovery (Restore from age-encrypted backup)"
echo "  2) Fresh Start   (Clone repos, no backup restoration)"
echo "======================================================="
while true; do
    read -r -p "Select an option [1-2]: " choice < /dev/tty
    case $choice in
        1) MODE="RESTORE"; break ;;
        2) MODE="FRESH"; break ;;
        *) echo "⚠️ Invalid option." ;;
    esac
done
echo ""

# --- PHASE 1A: RESTORE ---
if [[ "$MODE" == "RESTORE" ]]; then
    sudo [ -f "$AGE_KEYFILE" ] || { echo "❌ ERROR: Decryption key not found at $AGE_KEYFILE"; exit 1; }

    BACKUP=$(ls -t "$HOME"/docker-stacks-*.tar.zst.age 2>/dev/null | head -1 || true)
    [[ -z "$BACKUP" ]] && { echo "❌ ERROR: No backup archive found in $HOME"; exit 1; }

    echo ">>> Installing age & zstd..."
    sudo apt-get update -qq && sudo apt-get install -y -qq zstd age

    echo ">>> Decrypting $BACKUP..."
    sudo age -d -i "$AGE_KEYFILE" "$BACKUP" | sudo tar --zstd --same-owner --numeric-owner --transform="s,^home/[^/]\+,${HOME#/}," -xf - -C /

    echo ">>> Fixing extracted file ownership..."
    if [ -f "/tmp/backup-uid.txt" ]; then
        IFS=: read -r B_UID B_GID < /tmp/backup-uid.txt
        sudo find "$DIR_STACKS" "$DIR_SCRIPTS" "$DIR_CTRL" "$HOME/.ssh" \
            \( -uid "$B_UID" -o -gid "$B_GID" \) ! \( -uid "$(id -u)" -a -gid "$(id -g)" \) \
            -exec chown "$(id -u):$(id -g)" {} + 2>/dev/null || true
        sudo rm -f /tmp/backup-uid.txt
    fi
fi

# --- PHASE 1B: FRESH ---
if [[ "$MODE" == "FRESH" ]]; then
    PRIVATE_KEYS=$(find "$HOME/.ssh" -maxdepth 1 -type f -name "*.pub" | while read -r pub; do
        priv="${pub%.pub}"
        [[ -f "$priv" ]] && echo "$priv"
    done | wc -l)

    if [[ "$PRIVATE_KEYS" -eq 0 ]]; then
        echo "❌ ERROR: No SSH private keys found in $HOME/.ssh." >&2
        echo "   Please place your private key(s) in $HOME/.ssh before running a Fresh Start." >&2
        exit 1
    fi

    echo ">>> Found $PRIVATE_KEYS SSH private key(s). Proceeding..."
fi

# --- PHASE 2: SYSTEM PREP ---
echo ">>> Fixing SSH permissions..."
mkdir -p "$HOME/.ssh"
sudo chown -R "$(id -u):$(id -g)" "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
find "$HOME/.ssh" -type f -exec chmod 600 {} +
chmod 644 "$HOME/.ssh"/*.pub 2>/dev/null || true

echo ">>> Removing cloud-init..."
sudo apt-get purge -y -qq cloud-init
sudo rm -rf /etc/cloud /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo systemctl restart ssh || true

# --- PHASE 3: REPOSITORIES ---
setup_repo() {
    echo "🔗 Linking $1..."
    sudo mkdir -p "$1"

    [[ -z "$(ls -A "$1" 2>/dev/null)" ]] && sudo chown -R "$(id -u):$(id -g)" "$1"

    if [ -d "$1/.git" ]; then
        echo "   -> Restored repository detected. Syncing new remote commits safely..."
        git -C "$1" remote set-url origin "$2" 2>/dev/null || git -C "$1" remote add origin "$2"
        git -C "$1" fetch origin -q || return 1
        git -C "$1" pull origin main --rebase --autostash || echo "   ⚠️  Conflict detected in $(basename "$1"). Left for manual merge later."
    else
        echo "   -> Fresh start. Initializing and cloning..."
        git -C "$1" init -b main -q
        git -C "$1" remote add origin "$2"
        git -C "$1" fetch origin || return 1
        git -C "$1" checkout -f -B main origin/main -q
    fi
}

echo ">>> Syncing repositories..."
LINK_SUCCESS=true

set +e

setup_repo "$DIR_SCRIPTS" "git@${GIT_HOST}:${GIT_USER}/${REPO_SCRIPTS}.git" || \
setup_repo "$DIR_SCRIPTS" "git@${GIT_HOST_FALLBACK}:${GIT_USER}/${REPO_SCRIPTS}.git" || LINK_SUCCESS=false

setup_repo "$DIR_CTRL" "git@${GIT_HOST}:${GIT_USER}/${REPO_CTRL}.git" || \
setup_repo "$DIR_CTRL" "git@${GIT_HOST_FALLBACK}:${GIT_USER}/${REPO_CTRL}.git" || LINK_SUCCESS=false

setup_repo "$DIR_STACKS" "git@${GIT_HOST}:${GIT_USER}/${REPO_STACKS}.git" || \
setup_repo "$DIR_STACKS" "git@${GIT_HOST_FALLBACK}:${GIT_USER}/${REPO_STACKS}.git" || LINK_SUCCESS=false

set -e

# --- PHASE 4: CLEANUP & SUMMARY ---
if [[ "$MODE" == "RESTORE" && -n "${BACKUP:-}" ]]; then
    echo ">>> Cleaning up backup archive..."
    rm -- "$BACKUP"
fi

WARNING_MSG=""
[[ "$LINK_SUCCESS" == false ]] && WARNING_MSG=$'\n                         (⚠️ Linking failed! Re-run this script , select the "Fresh Start", and try again)'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
if [[ "$SCRIPT_DIR" == "$HOME" ]]; then
    rm -f "${BASH_SOURCE[0]}"
    echo ">>> Removed bootstrap script from $HOME."
fi

cat <<EOF

✅ Bootstrap phase complete!
Next steps:
  1. Run the installer:  ${DIR_SCRIPTS}/run_once/setup.sh${WARNING_MSG}
  2. Re-open your SSH session.
EOF