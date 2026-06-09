#!/bin/bash
# @DESCRIPTION: Phase 1 Bootstrap: Decrypts & restores a Day-0 archive, fixes SSH permissions, removes cloud-init and re-links blueprint git repositories.
# @FREQUENCY: Run Once (Disaster Recovery)

set -euo pipefail

[[ $EUID -eq 0 ]] && { echo "ERROR: Don't run as root." >&2; exit 1; }

AGE_KEYFILE="/root/.backup-key.txt"
EXTRACTED=false

# Helper function
confirm_skip() {
    read -r -p "$1 (y/n): " choice < /dev/tty
    [[ "$choice" == [yY]* ]] || { echo "Aborting..."; exit 1; }
    echo "Skipping backup restoration phase..."
}

if sudo [ -f "$AGE_KEYFILE" ]; then
    BACKUP=$(ls -t "$HOME"/docker-stacks-*.tar.zst.age 2>/dev/null | head -1 || true)
    if [[ -n "$BACKUP" ]]; then
        read -r -p "📦 Found backup: $BACKUP. Wanna proceed restoring it? (y/n): " choice < /dev/tty
        if [[ "$choice" == [yY]* ]]; then
            echo ">>> Installing age & zstd..."
            sudo apt-get update -qq && sudo apt-get install -y -qq zstd age
            echo ">>> Decrypting $BACKUP..."
            sudo age -d -i "$AGE_KEYFILE" "$BACKUP" | sudo tar --zstd --same-owner --numeric-owner -xf - -C /

            echo ">>> Fixing extracted file ownership..."
            IFS=: read -r B_UID B_GID < /tmp/backup-uid.txt
            sudo find "/opt/stacks" "$HOME/scripts" "$HOME/ctrl_s_master" "$HOME/.ssh" \
                -uid "$B_UID" ! -uid "$(id -u)" -exec chown "$(id -u):$(id -g)" {} +
            sudo rm -f /tmp/backup-uid.txt
            EXTRACTED=true
        else
            echo "Skipping backup restoration phase..."
        fi
    else
        confirm_skip "⚠️  Key found at $AGE_KEYFILE, but no backup archive found at $HOME - Sure you wanna skip?"
    fi
else
    confirm_skip "⚠️  $AGE_KEYFILE doesn't exist! Sure you wanna skip the backup restoration phase?"
fi

echo ">>> Fixing SSH permissions..."
mkdir -p "$HOME/.ssh"
sudo chown -R "$(id -u):$(id -g)" "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
chmod 600 "$HOME/.ssh"/id_* 2>/dev/null || echo "⚠️  No private keys found — skipping."
chmod 644 "$HOME/.ssh"/id_*.pub 2>/dev/null || true

echo ">>> Removing cloud-init..."
sudo apt-get purge -y -qq cloud-init
sudo rm -rf /etc/cloud /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo systemctl restart ssh || true

setup_repo() {
    echo "🔗 Linking $1..."
    sudo mkdir -p "$1"
    [[ -z "$(ls -A "$1" 2>/dev/null)" ]] && sudo chown -R "$(id -u):$(id -g)" "$1"
    git -C "$1" init -b main -q
    git -C "$1" remote set-url origin "$2" 2>/dev/null || git -C "$1" remote add origin "$2"
    git -C "$1" fetch origin || return 1
    git -C "$1" checkout -f -B main origin/main -q
}

echo ">>> Syncing repositories..."
LINK_SUCCESS=true
setup_repo "$HOME/scripts"       "git@codeberg.org:gravi-ctrl/homelab-blueprint.git" && \
setup_repo "$HOME/ctrl_s_master" "git@codeberg.org:gravi-ctrl/ctrl-s-master.git" && \
setup_repo "/opt/stacks"         "git@codeberg.org:gravi-ctrl/server-docker-backup.git" || LINK_SUCCESS=false

if [[ "$LINK_SUCCESS" == true ]]; then
    echo "✅ All repositories successfully linked!"
else
    echo "❌ Re-linking failed (Codeberg might be unreachable)."
fi

echo ">>> Cleaning up..."
[[ "$EXTRACTED" == true ]] && rm -- "$BACKUP"

WARNING_MSG=""
[[ "$LINK_SUCCESS" == false ]] && WARNING_MSG=$'\n         (⚠️ Linking failed! Re-run this script and skip restoration)'

cat <<EOF
✅ Bootstrap phase complete!
Next steps:
  1. Run the installer:  ~/scripts/run_once/setup.sh${WARNING_MSG}
  2. Re-open your SSH session.
EOF