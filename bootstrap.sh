#!/bin/bash

# @DESCRIPTION: Phase 1 Bootstrap: Installs recovery tools, decrypts the Day-0 archive, restores filesystem state, and preps SSH.
# @FREQUENCY: Run Once (Disaster Recovery)

set -euo pipefail

# Decrypts weekly backup, fixes perms, preps for setup.sh

[[ $EUID -eq 0 ]] && { echo "ERROR: Don't run as root." >&2; exit 1; }

KEY="/root/.backup-key.txt"
sudo test -f "$KEY" || { echo "ERROR: Key not found at $KEY" >&2; exit 1; }

BACKUP="$(ls -t "$HOME"/docker-stacks-*.tar.zst.age 2>/dev/null | head -1 || true)"
[[ -z "$BACKUP" ]] && { echo "ERROR: No backup archive found in $HOME" >&2; exit 1; }
echo "Using backup: $BACKUP"

echo ">>> Installing age and zstd..."
sudo apt-get update -qq && sudo apt-get install -y -qq zstd age

echo ">>> Decrypting and extracting backup..."
sudo age -d -i "$KEY" "$BACKUP" | sudo tar --zstd -xf - -C /

echo ">>> Fixing SSH permissions..."
sudo chown -R "$(id -u):$(id -g)" "$HOME/.ssh"
chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh"/id_* && chmod 644 "$HOME/.ssh"/id_*.pub

echo ">>> Removing cloud-init..."
sudo apt-get purge -y -qq cloud-init
sudo rm -rf /etc/cloud /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo systemctl restart ssh

echo ">>> Re-linking the repos..."
setup_repo() {
    local target_dir=$1
    local repo_url=$2
    
    echo "Linking $target_dir with $repo_url..."
    (cd "$target_dir" && \
     git init -b main && \
     git remote add origin "$repo_url" 2>/dev/null || git remote set-url origin "$repo_url" && \
     git fetch origin && \
     git checkout -f -B main origin/main)
}

setup_repo "$HOME/scripts" "git@codeberg.org:gravi-ctrl/homelab-blueprint.git"
setup_repo "$HOME/ctrl_s_master" "git@codeberg.org:gravi-ctrl/ctrl-s-master.git"
setup_repo "/opt/stacks" "git@codeberg.org:gravi-ctrl/server-docker-backup.git"

echo ">>> Cleaning up..."
rm -- "$0" "$BACKUP"

cat <<'EOF'

✅ Bootstrap complete!

Next steps:
  1. Run the installer:  ~/scripts/run_once/setup.sh

  2. Re-open your SSH session.
EOF
