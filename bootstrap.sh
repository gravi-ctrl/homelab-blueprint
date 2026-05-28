#!/bin/bash

# @DESCRIPTION: Phase 1 Bootstrap: Preps SSH, optionally restores a Day-0 archive, and auto-links blueprint git repositories.
# @FREQUENCY: Run Once (Disaster Recovery)

set -euo pipefail

# Decrypts weekly backup, fixes perms, preps for setup.sh

[[ $EUID -eq 0 ]] && { echo "ERROR: Don't run as root." >&2; exit 1; }

KEY="/root/.backup-key.txt"
EXTRACTED=false

if sudo test -f "$KEY"; then
    BACKUP="$(ls -t "$HOME"/docker-stacks-*.tar.zst.age 2>/dev/null | head -1 || true)"
    [[ -z "$BACKUP" ]] && { echo "ERROR: Key found at $KEY, but no backup archive found!" >&2; exit 1; }
    
    echo "Using backup: $BACKUP"
    echo ">>> Installing age and zstd..."
    sudo apt-get update -qq && sudo apt-get install -y -qq zstd age

    echo ">>> Decrypting and extracting backup..."
    sudo age -d -i "$KEY" "$BACKUP" | sudo tar --zstd -xf - -C /
    
    EXTRACTED=true
else
    read -r -p "⚠️  Key doesn't exist! Sure you wanna skip the backup restoration phase? (y/n): " choice
    case "$choice" in
        y|Y) echo "Skipping backup restoration phase..." ;;
        *) echo "Aborting..."; exit 1 ;;
    esac
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
sudo systemctl restart ssh

echo ">>> Generating self-destructing re-link script..."
cat << 'EOF' > "$HOME/re-link.sh"
#!/bin/bash

setup_repo() {
    echo "🔗 Linking $1..."
    
    sudo mkdir -p "$1"
    sudo chown -R "$(id -u):$(id -g)" "$1"
    
    git -C "$1" init -b main -q
    git -C "$1" remote set-url origin "$2" 2>/dev/null || git -C "$1" remote add origin "$2"
    git -C "$1" fetch origin || return 1
    git -C "$1" checkout -f -B main origin/main -q
}

echo ">>> Syncing repositories..."

if setup_repo "$HOME/scripts"       "git@codeberg.org:gravi-ctrl/homelab-blueprint.git" &&
   setup_repo "$HOME/ctrl_s_master" "git@codeberg.org:gravi-ctrl/ctrl-s-master.git" &&
   setup_repo "/opt/stacks"         "git@codeberg.org:gravi-ctrl/server-docker-backup.git"; then

    echo "✅ All repositories successfully linked!"
    rm -- "$0"

else
    echo "❌ Re-linking failed (Codeberg might be unreachable)."
    echo "⚠️  Try running '~/re-link.sh' manually later once the connection is restored."
    exit 1
fi
EOF

chmod +x "$HOME/re-link.sh"

"$HOME/re-link.sh" || true

echo ">>> Cleaning up..."
if [[ "$EXTRACTED" == true ]]; then
    rm -- "$BACKUP"
fi

cat <<'EOF'

✅ Bootstrap phase complete!

Next steps:
  1. Run the installer:  ~/scripts/run_once/setup.sh
         (If linking failed, run ~/re-link.sh first!)

  2. Re-open your SSH session.
EOF