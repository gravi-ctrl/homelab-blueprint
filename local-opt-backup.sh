#!/bin/bash
# @DESCRIPTION: Backs up Docker volumes to tar.zst, backs up `~/.ssh` and `/etc/ssh`
# @FREQUENCY: Weekly 5:30am on Monday (root crontab)
# ==============================================================================
# 🛡️  SERVER BACKUP
# ==============================================================================
# 🆘 RESTORE INSTRUCTIONS
#
# --- DOCKER RESTORE ---
# 1.  Stop Docker: sudo systemctl stop docker
# 2.  Extract:     sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C /
# 3.  Permissions: sudo chown -R $(id -u):$(id -g) ~/.ssh
# 3.5 Permissions: sudo chmod 700 ~/.ssh
#                  sudo chmod 600 ~/.ssh/id_*
#                  sudo chmod 644 ~/.ssh/id_*.pub
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
trap 'kill $HEARTBEAT_PID 2>/dev/null; systemctl unmask docker.socket; systemctl start containerd docker.socket docker.service' EXIT

echo "Stopping and Masking Docker..."
systemctl mask docker.socket
systemctl stop docker.socket docker.service containerd

echo "Waiting 20 seconds for clean shutdown..."
sleep 20

echo "Creating high-speed backup (ZSTD)..."
tar --use-compress-program="zstd -3 -T0" -cf "$BACKUP_DIR/$DOCKER_FILENAME" \
    \
    `# --- Generic patterns ---` \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='lost+found' \
    --exclude='*.log' \
    --exclude='*.log.gz' \
    --exclude='*.log.??' \
    --exclude='*.tmp' \
    --exclude='*.pyc' \
    --exclude='*.pid' \
    --exclude='*.swp' \
    --exclude='*.bak' \
    --exclude='*.js.map' \
    --exclude='*.sock' \
    --exclude='*.core' \
    --exclude='ipc-socket' \
    --exclude='lockfile' \
    --exclude='GPUCache' \
    --exclude='CachedImages' \
    --exclude='Crash Reports' \
    \
    `# --- Stack-specific ---` \
    --exclude='opt/stacks/jellyfin/config/transcodes' \
    --exclude='opt/stacks/jellyfin/config/cache' \
    --exclude='opt/stacks/jellyfin/config/log' \
    --exclude='opt/stacks/arrs/config/*/MediaCover' \
    --exclude='opt/stacks/arrs/config/*/Backups' \
    --exclude='opt/stacks/arrs/config/*/logs' \
    --exclude='opt/stacks/arrs/config/*/UpdateLogs' \
    --exclude='opt/stacks/scrutiny/influxdb' \
    --exclude='opt/stacks/audiobookshelf/backups' \
    --exclude='opt/stacks/audiobookshelf/metadata/cache' \
    --exclude='opt/stacks/pihole/etc-pihole/pihole-FTL.db' \
    --exclude='opt/stacks/pihole/etc-pihole/gravity_old.db' \
    --exclude='opt/stacks/qbittorrent/config/qBittorrent/data/logs' \
    --exclude='opt/stacks/qbittorrent/config/qBittorrent/data/GeoDB' \
    --exclude='opt/stacks/jdownloader/config/logs' \
    --exclude='opt/stacks/jdownloader/config/tmp' \
    --exclude='opt/stacks/borg-ui/borg_cache' \
    \
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
STUCK_CONTAINERS=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -iE "restarting|exited|unhealthy" | awk '{print $1}')

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
