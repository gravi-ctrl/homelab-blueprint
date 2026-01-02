#!/bin/bash

# --- CONFIGURATION START ---
TARGET_USER="gravi-ctrl"

read -r -d '' PRIVATE_KEY << 'EOF'
-----BEGIN OPENSSH PRIVATE KEY-----
... PASTE KEY HERE ...
-----END OPENSSH PRIVATE KEY-----
EOF

read -r -d '' PUBLIC_KEY << 'EOF'
ssh-ed25519 AAAAC3... comment
EOF
# --- CONFIGURATION END ---

# 0. Safety Check: Am I Root?
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo)."
  exit 1
fi

# 0. Safety Check: Does the user exist?
if ! id "$TARGET_USER" &>/dev/null; then
    echo "Error: User '$TARGET_USER' does not exist on this system."
    echo "Please run: sudo useradd -m $TARGET_USER"
    exit 1
fi

echo "Starting SSH Key Restore for user: $TARGET_USER"

# 1. Setup Directories
HOMEDIR="/home/$TARGET_USER"
SSH_DIR="$HOMEDIR/.ssh"

# Ensure home exists (in case user exists but folder doesn't)
if [ ! -d "$HOMEDIR" ]; then
    mkdir -p "$HOMEDIR"
fi

mkdir -p "$SSH_DIR"

# 2. Write Keys
# The -e flag ensures formatting/newlines are preserved
echo "$PRIVATE_KEY" > "$SSH_DIR/id_ed25519"
echo "$PUBLIC_KEY" > "$SSH_DIR/id_ed25519.pub"
echo "$PUBLIC_KEY" > "$SSH_DIR/authorized_keys"

echo "Keys written."

# 3. Fix Permissions
chmod 700 "$SSH_DIR"
chmod 600 "$SSH_DIR/id_ed25519"
chmod 600 "$SSH_DIR/authorized_keys"
chmod 644 "$SSH_DIR/id_ed25519.pub"

# 4. Automate Known Hosts (Scanning)
KNOWN_HOSTS="$SSH_DIR/known_hosts"
touch "$KNOWN_HOSTS"
chmod 644 "$KNOWN_HOSTS"

echo "Scanning Git providers..."
# We clear old keys for these specific hosts to avoid duplicates if script runs twice
ssh-keygen -f "$KNOWN_HOSTS" -R github.com 2>/dev/null
ssh-keygen -f "$KNOWN_HOSTS" -R gitlab.com 2>/dev/null

ssh-keyscan -H github.com 2>/dev/null >> "$KNOWN_HOSTS"
ssh-keyscan -H gitlab.com 2>/dev/null >> "$KNOWN_HOSTS"

# 5. Fix Ownership (Recursive)
# Do this LAST to ensure everything (including known_hosts) is owned by the user
chown -R "$TARGET_USER:$TARGET_USER" "$SSH_DIR"

echo "---------------------------------------------------"
echo "SUCCESS! Keys restored for $TARGET_USER."
echo "IMPORTANT: Delete this script file now to protect your private key."
echo "---------------------------------------------------"
