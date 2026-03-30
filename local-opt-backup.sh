#!/bin/bash
# @DESCRIPTION: Backs up Docker volumes to tar.zst, backs up ~/scripts, ~/.ssh and /etc/ssh
# @FREQUENCY: Weekly 5:30am on Thursday (root crontab)
# ==============================================================================
# RESTORE:
#   1. Stop Docker:         sudo systemctl stop docker
#   2. Decrypt & Extract:   sudo age -d -i /root/.backup-key.txt docker-stacks-*.tar.zst.age | sudo tar --use-compress-program=zstd -xf - -C /
#   3. Restart SSH:         sudo systemctl restart ssh
#   4. Start Docker:        sudo systemctl start docker
# ==============================================================================
set -o pipefail

# --- INTERACTIVITY CHECK ---
# If running manually, switch to SystemD background service.
if [ -t 0 ]; then
    echo "⚠️  Interactive session detected. Switching to background SystemD service..."

    if command -v systemd-run &> /dev/null; then
        UNIT_NAME="docker-backup-manual-$(date +%s)"
        systemd-run --unit="$UNIT_NAME" \
                    --quiet \
                    "$(realpath "${BASH_SOURCE[0]}")"

        echo "✅ Backup dispatched to background. You may safely disconnect."
        echo "📝 Monitor logs: journalctl -u $UNIT_NAME -f"
        echo "🛑 Stop backup: systemctl stop $UNIT_NAME"
        exit 0
    else
        echo "❌ systemd-run not found. Proceeding in foreground (Do not close SSH)."
        sleep 3
    fi
fi

LOCKFILE="/tmp/local-opt-backup.lock"
AGE_PUBKEY=[REDACTED]
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="/data/assets/syncthing/Backup/self-hosted/docker-containers-backup"
STACKS_ROOT="/opt/stacks"
DATE=$(date +%F)
DOCKER_FILENAME="docker-stacks-$DATE.tar.zst.age"
RUNNING_STACKS_FILE="/tmp/running-stacks.list"

# --- PREVENT CONCURRENT RUNS ---
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "⚠️  Backup already running. Exiting."
    exit 0
fi
echo $$ > "$LOCKFILE"

# --- CONFIGURATION ---
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
if ! command -v zstd &> /dev/null; then
    apt-get update && apt-get install -y zstd
fi

if ! command -v age &> /dev/null; then
    apt-get update && apt-get install -y age
fi

mkdir -p "$BACKUP_DIR"

# HEARTBEAT FUNCTION
keep_kuma_alive() {
    while true; do
        curl -fsS --retry 3 "$KUMA_HC_URL" > /dev/null
        sleep 240
    done
}

# SAFETY & CLEANUP FUNCTION
cleanup() {
    # 1. Remove lock & stacks snapshot
    rm -f "$LOCKFILE"
    rm -f "$RUNNING_STACKS_FILE"

    # 2. Kill Heartbeat
    if [ -n "$HEARTBEAT_PID" ]; then
        kill $HEARTBEAT_PID 2>/dev/null
        wait $HEARTBEAT_PID 2>/dev/null
    fi

    # 3. Ensure Docker is unmasked and started
    # Only restore Docker if it's not already running
    if ! docker info &>/dev/null; then
        echo "Restoring Docker Services..."
        systemctl unmask docker.socket 2>/dev/null
        systemctl start containerd docker.socket docker.service 2>/dev/null
    fi
}

# Trap signals: EXIT (Success/Fail), INT (Ctrl+C), TERM (Kill), HUP (Disconnect)
trap cleanup EXIT INT TERM HUP

echo "--- [1/2] Starting Docker Stacks Backup ---"

keep_kuma_alive &
HEARTBEAT_PID=$!

# Snapshot running stacks before shutdown
> "$RUNNING_STACKS_FILE"
for stack_dir in "$STACKS_ROOT"/*/; do
    if [ -f "$stack_dir/docker-compose.yml" ]; then
        if docker compose -f "$stack_dir/docker-compose.yml" ps -q --status running 2>/dev/null | grep -q .; then
            echo "$stack_dir" >> "$RUNNING_STACKS_FILE"
        fi
    fi
done
echo "Captured $(wc -l < "$RUNNING_STACKS_FILE") running stack(s)."

echo "Stopping and masking Docker..."
systemctl mask docker.socket
systemctl stop docker.socket docker.service containerd

echo "Creating backup (ZSTD)..."

timeout 60m tar --use-compress-program="zstd -3 -T0" -cf - \
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
    --exclude="home/$BACKUP_USER/scripts/ctrl_s_master/venv" \
    --exclude="home/$BACKUP_USER/scripts/ctrl_s_master/_logs" \
    --exclude="home/$BACKUP_USER/scripts/ctrl_s_master/vaults.hc" \
    --exclude="home/$BACKUP_USER/scripts/ctrl_s_master/status.json" \
    --exclude="home/$BACKUP_USER/scripts/ctrl_s_master/status_dashboard.md" \
    -C / \
    opt/stacks \
    "home/$BACKUP_USER/scripts" \
    "home/$BACKUP_USER/.ssh" \
    etc/ssh \
| age -e -r "$AGE_PUBKEY" -o "$BACKUP_DIR/$DOCKER_FILENAME"

TAR_EXIT_CODE=${PIPESTATUS[0]}

# Explicit restart for immediate healthchecks
systemctl unmask docker.socket
systemctl start containerd docker.socket docker.service

echo "Waiting for Docker socket..."
for i in $(seq 1 30); do
    docker info &>/dev/null && break
    sleep 2
done

if ! docker info &>/dev/null; then
    echo "❌ Docker failed to start after 60s!"
    exit 1
fi

echo "Starting previously-running stacks..."
if [ -f "$RUNNING_STACKS_FILE" ]; then
    while IFS= read -r stack_dir; do
        if [ -f "$stack_dir/docker-compose.yml" ]; then
            echo "  → $(basename "$stack_dir")"
            docker compose -f "$stack_dir/docker-compose.yml" up -d
        fi
    done < "$RUNNING_STACKS_FILE"
    rm -f "$RUNNING_STACKS_FILE"
else
    echo "⚠️  No snapshot found. Skipping stack startup."
fi

# Validation
if [ $TAR_EXIT_CODE -eq 0 ]; then
    echo "✅ Backup successful."
    echo "Verifying backup integrity..."
    if age -d -i /root/.backup-key.txt "$BACKUP_DIR/$DOCKER_FILENAME" | zstd -t; then
        echo "✅ Backup verified (decryption + integrity)"
        find "$BACKUP_DIR" -type f -name "docker-stacks-*.tar.zst.age" \
            ! -name "$DOCKER_FILENAME" -printf '%T@ %p\n' \
            | sort -n | head -n -1 | awk '{print $2}' | xargs -r rm
    else
        echo "❌ Backup CORRUPT or key mismatch."
        exit 1
    fi
elif [ $TAR_EXIT_CODE -eq 124 ]; then
    echo "❌ Backup TIMED OUT (>60m). Docker restarted."
    exit 1
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
    STUCK=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -iE "exited|unhealthy|restarting" | awk '{print $1}')
    if [ -z "$STUCK" ]; then
        echo "✅ All containers healthy."
        break
    fi
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

if [ -n "$STUCK" ]; then
    for container in $STUCK; do
        docker restart "$container"
    done
fi

echo "🎉 All tasks finished."
exit 0
