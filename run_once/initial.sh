#!/bin/bash
# @DESCRIPTION: Makes sure zstd is installed, extracts the `docker-stacks-DATE.tar.zst` backup and fixes SSH permissions (Run without sudo)
# @FREQUENCY: Run Once

set -euo pipefail

BACKUP_FILES=("$HOME"/docker-stacks-*.tar.zst)

if [[ ${#BACKUP_FILES[@]} -eq 0 || ! -e "${BACKUP_FILES[0]}" ]]; then
    echo "ERROR: No docker-stacks-*.tar.zst found in $HOME" >&2
    exit 1
fi

if [[ ${#BACKUP_FILES[@]} -gt 1 ]]; then
    echo "ERROR: Multiple backup files found, expected exactly one:" >&2
    printf '  %s\n' "${BACKUP_FILES[@]}" >&2
    exit 1
fi

BACKUP="${BACKUP_FILES[0]}"
echo "Using backup: $BACKUP"

sudo apt-get install -y zstd
sudo tar --use-compress-program=zstd -xf "$BACKUP" -C /
sudo chown -R "$(id -u):$(id -g)" "$HOME/.ssh"
chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh"/id_* && chmod 644 "$HOME/.ssh"/id_*.pub

# Remove cloud-init (not needed on bare metal, creates conflicting SSH configs)
sudo apt purge cloud-init -y
sudo rm -rf /etc/cloud
sudo rm -f /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo systemctl restart ssh
