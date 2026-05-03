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

echo ">>> Cleaning up..."
rm -- "$0" "$BACKUP"

cat <<'EOF'

✅ Bootstrap complete!

Next steps:
  1. Re-link scripts repo:
       cd ~/scripts && git init && git remote add origin git@codeberg.org:gravi-ctrl/homelab-blueprint.git && git fetch origin && git checkout -f -B main origin/main

  2. Run the installer:  ~/scripts/run_once/setup.sh

  3. Re-open your SSH session.
EOF
