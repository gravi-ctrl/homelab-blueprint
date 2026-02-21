#!/bin/bash
# @DESCRIPTION: Backs up Docker volumes to tar.zst, backs up ~/.ssh and /etc/ssh
# @FREQUENCY: Weekly 5:30am on Thursday (root crontab)
# ==============================================================================
# RESTORE:
#   1. Stop Docker:  sudo systemctl stop docker
#   2. Extract:      sudo tar --use-compress-program=zstd -xf docker-stacks-DATE.tar.zst -C /
#   3. Fix perms:    sudo chown -R $(id -u):$(id -g) ~/.ssh
#   4. SSH perms:    sudo chmod 700 ~/.ssh && chmod 600 ~/.ssh/id_* && chmod 644 ~/.ssh/id_*.pub
#   5. Start Docker: sudo systemctl start docker
# ==============================================================================
set -o pipefail

# --- 1. CONFIGURATION & SECRETS ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    source "$SCRIPT_DIR/.env"
else
    echo "❌ .env file not found in $SCRIPT_DIR"
    exit 1
fi

if [ -z "$KUMA_HC_URL" ] || [ -z "$BACKUP_USER" ]; then
    echo "❌ Missing configuration in .env"
    exit 1
fi

if [ ! -d "/home/$BACKUP_USER/.ssh" ]; then
    echo "❌ /home/$BACKUP_USER/.ssh does not exist"
    exit 1
fi

# Install dependencies if missing
if ! command -v zstd &> /dev/null || ! command -v at &> /dev/null; then
    echo "Installing dependencies (zstd, at)..."
    apt-get update && apt-get install -y zstd at
fi

BACKUP_DIR="/srv/data/assets/syncthing/Backup/docker-containers-backup"
STACKS_ROOT="/opt/stacks"
DATE=$(date +%F)
DOCKER_FILENAME="docker-stacks-$DATE.tar.zst"

mkdir -p "$BACKUP_DIR"

# HEARTBEAT FUNCTION (Runs in background)
keep_kuma_alive() {
    while true; do
        curl -fsS --retry 3 "$KUMA_HC_URL" > /dev/null
        sleep 240
    done
}

echo "--- [1/2] Starting Docker Stacks Backup ---"

keep_kuma_alive &
HEARTBEAT_PID=$!

# SAFETY & CLEANUP FUNCTION
cleanup() {
    # 1. Kill the Heartbeat immediately
    kill $HEARTBEAT_PID 2>/dev/null
    
    # 2. Ensure Docker is unmasked and started
    systemctl unmask docker.socket 2>/dev/null
    systemctl start containerd docker.socket docker.service 2>/dev/null

    # 3. Cancel the "Dead Man's Switch" (at jobs)
    for job in $(atq | awk '{print $1}'); do atrm $job; done 2>/dev/null
}

# Trap signals: EXIT (normal/error), INT (Ctrl+C), TERM (kill)
trap cleanup EXIT INT TERM

echo "Stopping and masking Docker..."

# DEAD MAN'S SWITCH: Schedule auto-rescue in 1 hour.
echo "kill $HEARTBEAT_PID 2>/dev/null; systemctl unmask docker.socket; systemctl start containerd docker.socket docker.service" | at now + 60 minutes 2>/dev/null

systemctl mask docker.socket
systemctl stop docker.socket docker.service containerd

echo "Creating backup (ZSTD)..."
tar --use-compress-program="zstd -3 -T0" -cf "$BACKUP_DIR/$DOCKER_FILENAME" \
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
    -C / \
    opt/stacks \
    "home/$BACKUP_USER/.ssh" \
    etc/ssh

TAR_EXIT_CODE=$?

echo "--- [2/2] Restoring Services & Cleanup ---"
systemctl unmask docker.socket
systemctl start containerd docker.socket docker.service

echo "Waiting for Docker socket..."
sleep 20

echo "Starting stacks..."
for stack_dir in "$STACKS_ROOT"/*/; do
    if [ -f "$stack_dir/docker-compose.yml" ]; then
        echo "  → $(basename "$stack_dir")"
        docker compose -f "$stack_dir/docker-compose.yml" up -d
    fi
done

# Validation
if [ $TAR_EXIT_CODE -eq 0 ] || [ $TAR_EXIT_CODE -eq 1 ]; then
    [ $TAR_EXIT_CODE -eq 1 ] && echo "⚠️ Backup completed with warnings." || echo "✅ Backup successful."

    echo "Verifying backup integrity..."
    if zstd -t "$BACKUP_DIR/$DOCKER_FILENAME"; then
        BACKUP_SIZE=$(du -sh "$BACKUP_DIR/$DOCKER_FILENAME" | cut -f1)
        echo "📦 Backup size: $BACKUP_SIZE"
        find "$BACKUP_DIR" -type f -name "docker-stacks-*.tar.zst" \
            ! -name "$DOCKER_FILENAME" -printf '%T@ %p\n' \
            | sort -n | head -n -1 | awk '{print $2}' | xargs -r rm
    else
        echo "❌ Backup file is CORRUPT. Keeping old backups."
        exit 1
    fi
else
    echo "❌ Tar backup failed (Code $TAR_EXIT_CODE)!"
    exit 1
fi

# Healthcheck
echo "Waiting for containers to settle..."
MAX_WAIT=120
ELAPSED=0
INTERVAL=10

while [ $ELAPSED -lt $MAX_WAIT ]; do
    STUCK=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -iE "exited|unhealthy" | awk '{print $1}')

    if [ -z "$STUCK" ]; then
        echo "✅ All containers healthy after ${ELAPSED}s"
        break
    fi

    echo "  ⏳ ${ELAPSED}s — still waiting on: $STUCK"
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

if [ -n "$STUCK" ]; then
    for container in $STUCK; do
        echo "Restarting stuck container: $container"
        docker restart "$container"
    done
fi

echo "🎉 All tasks finished."
exit 0
