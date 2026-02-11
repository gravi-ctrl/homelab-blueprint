#!/bin/bash
# @DESCRIPTION: Backs up Docker volumes to tar.zst, backs up `~/.ssh` and `/etc/ssh`
# @FREQUENCY: Weekly (Mon 5:30am) (root crontab)
# ==============================================================================
# 🛡️  SERVER BACKUP
# ==============================================================================
# 🆘 RESTORE INSTRUCTIONS
#
# --- DOCKER RESTORE ---
# 1.  Stop Docker: sudo systemctl stop docker
# 2.  Extract:     sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C /
# 3.  Permissions: sudo chown -R $(id -u):$(id -g) /home/gravi-ctrl/.ssh
# 3.5 Permissions: sudo chmod 700 /home/gravi-ctrl/.ssh
#                  sudo chmod 600 /home/gravi-ctrl/.ssh/id_ed25519
# 4.  Start:       sudo systemctl start docker
# ==============================================================================

# --- 1. CONFIGURATION & SECRETS ---
# .env file stored beside the script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    source "$SCRIPT_DIR/.env"
else
    echo "❌ Error: .env file not found in $SCRIPT_DIR"
    exit 1
fi

# Ensure mandatory URL is loaded
if [ -z "$KUMA_HC_URL" ]; then
    echo "❌ Error: KUMA_HC_URL is not set in .env"
    exit 1
fi

# Auto-install zstd if missing
if ! command -v zstd &> /dev/null; then
    echo "Installing zstd..."
    apt-get update && apt-get install -y zstd
fi

BACKUP_DIR="/srv/data/assets/syncthing/Backup/docker-containers-backup"
STACKS_ROOT="/opt/stacks"
DATE=$(date +%F)
DOCKER_FILENAME="docker-stacks-$DATE.tar.zst"

mkdir -p "$BACKUP_DIR"

# --- HEARTBEAT FUNCTION (Runs in background) ---
keep_kuma_alive() {
    while true; do
        curl -fsS --retry 3 "$KUMA_HC_URL" > /dev/null
        sleep 240
    done
}

# --- 2. DOCKER STOP & BACKUP ---
echo "--- [1/2] Starting Docker Stacks Backup ---"

# Start the background heartbeat
keep_kuma_alive &
HEARTBEAT_PID=$!

# Safety Net: Kill heartbeat and restore Docker on exit
trap 'kill $HEARTBEAT_PID 2>/dev/null; systemctl unmask docker.socket; systemctl start docker.socket; systemctl start docker.service' EXIT

echo "Stopping and Masking Docker..."
systemctl mask docker.socket
systemctl stop docker.socket docker.service containerd

echo "Waiting 20 seconds for clean shutdown..."
sleep 20

echo "Creating high-speed backup (ZSTD)..."
tar --use-compress-program="zstd -3 -T0" -cf "$BACKUP_DIR/$DOCKER_FILENAME" \
    --exclude='.git' \
    --exclude='*.log' \
    --exclude='*.log.gz' \
    --exclude='*.tmp' \
    --exclude='*.js.map' \
    --exclude='*/nextcloud/data/appdata_*/preview/*' \
    --exclude='*/nextcloud/data/updater-*/backups/*' \
    -C / \
    opt/stacks \
    home/$BACKUP_USER/.ssh \
    etc/ssh

TAR_EXIT_CODE=$?

# --- 3. RESTORATION & SURGICAL REPAIR ---
echo "--- [2/2] Restoring Services & Cleanup ---"

echo "Unmasking and Starting Docker..."
systemctl unmask docker.socket
systemctl start containerd docker.socket docker.service

echo "Waiting 20 seconds for Docker socket..."
sleep 20

echo "Running initial stack convergence..."
find "$STACKS_ROOT" -name "docker-compose.yml" -execdir docker compose up -d \;

echo "Waiting 60 seconds for HDD I/O to settle..."
sleep 60

# Find containers that are not running or are unhealthy
STUCK_CONTAINERS=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -E "restarting|exited|unhealthy" | awk '{print $1}')

if [ ! -z "$STUCK_CONTAINERS" ]; then
    for container in $STUCK_CONTAINERS; do
        echo "Restarting stuck container: $container"
        docker restart "$container"
    done
fi

# --- 4. VALIDATION & CLEANUP ---
if [ $TAR_EXIT_CODE -eq 0 ] || [ $TAR_EXIT_CODE -eq 1 ]; then
    [ $TAR_EXIT_CODE -eq 1 ] && echo "⚠️ Backup completed with warnings." || echo "✅ Backup Successful."
    # Find and delete old tar.zst files, keep current one
    find "$BACKUP_DIR" -type f -name "docker-stacks-*.tar.zst" ! -name "$DOCKER_FILENAME" -delete
    echo "🎉 ALL TASKS FINISHED."
    exit 0
else
    echo "❌ Tar backup failed (Code $TAR_EXIT_CODE)!"
    exit 1
fi
