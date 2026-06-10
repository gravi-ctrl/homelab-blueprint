#!/bin/bash
# @DESCRIPTION: Backs up Docker stacks, `~/scripts`, `~/ctrl_s_master`, `~/.ssh`, /etc/ssh and $HOME/.local/share/mkcert to an age-encrypted tar.zst archive
# @FREQUENCY: Weekly 5:30am on Thursday (root crontab)
# @USES_ENV: BACKUP_DIR, STACKS_DIR, AGE_KEYFILE, KUMA_HC_URL, SCRIPTS_DIR, CTRL_DIR
# ==============================================================================
# RESTORE:
#   1. Stop Docker:         sudo systemctl stop docker
#   2. Decrypt & Extract:   sudo age -d -i /root/.backup-key.txt docker-stacks-*.tar.zst.age | sudo tar --zstd --same-owner --numeric-owner -xf - -C /
#   3. Restart SSH:         sudo systemctl restart ssh
#   4. Start Docker:        sudo systemctl start docker
# ==============================================================================
set -o pipefail
shopt -s nullglob

[[ $EUID -ne 0 ]] && { echo "❌ ERROR: This script must be run as root (or via sudo)." >&2; exit 1; }

# --- .ENV ---
if [ -f "/opt/scripts/.env" ]; then
    source "/opt/scripts/.env"
else
    echo "❌ .env file not found in /opt/scripts"
    exit 1
fi

# --- INTERACTIVITY CHECK ---
# If running manually, switch to SystemD background service.
if [ -t 0 ]; then
    echo "⚠️  Interactive session detected. Switching to background SystemD service..."

    if command -v systemd-run &> /dev/null; then
        UNIT_NAME="docker-backup-manual"

        # Clean up any previous failed run
        systemctl reset-failed "$UNIT_NAME" 2>/dev/null

        systemd-run --unit="$UNIT_NAME" \
                    --quiet \
                    --collect \
                    "$(realpath "${BASH_SOURCE[0]}")"

        echo "✅ Backup dispatched to background. You may safely disconnect."
        echo ""
        echo "📝 Follow live logs:        journalctl -u $UNIT_NAME -f"
        echo "📜 View last run:           journalctl -u $UNIT_NAME --since today"
        echo "📆 View previous Thursday:  journalctl -u $UNIT_NAME --since \"last Thursday\""
        echo "🔍 Check status:            systemctl status $UNIT_NAME"
        echo "🛑 Stop backup:             systemctl stop $UNIT_NAME"
        exit 0
    else
        echo "❌ systemd-run not found. Proceeding in foreground (Do not close SSH)."
        sleep 3
    fi
fi

# 1. Find script owner
SCRIPT_OWNER=$(stat -c '%U' "${BASH_SOURCE[0]}")
# 2. Get that owner's home directory
USER_HOME=$(getent passwd "$SCRIPT_OWNER" | cut -d: -f6)

LOCKFILE="/tmp/local-opt-backup.lock"
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
if [ ! -f "$AGE_KEYFILE" ]; then
    echo "❌ Age key file not found at $AGE_KEYFILE"
    exit 1
fi

if [ -z "$KUMA_HC_URL" ] || [ -z "$AGE_KEYFILE" ] || [ -z "$STACKS_DIR" ] || [ -z "$BACKUP_DIR" ]; then
    echo "❌ Missing configuration in .env"
    exit 1
fi

if [ ! -d "$USER_HOME" ]; then
    echo "❌ $USER_HOME does not exist"
    exit 1
fi

# Install dependencies if missing
if ! command -v zstd &> /dev/null; then
    apt-get update && apt-get install -y zstd
fi

if ! command -v age &> /dev/null; then
    apt-get update && apt-get install -y age
fi

# Derive public key from identity file (age must be installed first)
AGE_PUBKEY=$(age-keygen -y "$AGE_KEYFILE") || { echo "❌ Failed to read public key from $AGE_KEYFILE"; exit 1; }

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
    rm -f /tmp/backup-uid.txt

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
for stack_dir in "$STACKS_DIR"/*/; do
    if [ -f "$stack_dir/compose.yml" ]; then
        if docker compose -f "$stack_dir/compose.yml" ps -q --status running 2>/dev/null | grep -q .; then
            echo "$stack_dir" >> "$RUNNING_STACKS_FILE"
        fi
    fi
done
echo "Captured $(wc -l < "$RUNNING_STACKS_FILE") running stack(s)."

# Write backup UID/GID metadata
echo "$(id -u "$SCRIPT_OWNER"):$(id -g "$SCRIPT_OWNER")" > /tmp/backup-uid.txt

echo "Stopping stacks gracefully..."
if [ -f "$RUNNING_STACKS_FILE" ]; then
    while IFS= read -r stack_dir; do
        if [ -f "$stack_dir/compose.yml" ]; then
            echo "  → Stopping $(basename "$stack_dir")"
            docker compose -f "$stack_dir/compose.yml" down --timeout 30
        fi
    done < "$RUNNING_STACKS_FILE"
fi

echo "Stopping and masking Docker..."
systemctl mask docker.socket
systemctl stop docker.socket docker.service containerd

echo "Creating backup (ZSTD)..."

timeout 60m tar --use-compress-program="zstd -9 -T0 --long" -cf - \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='lost+found' \
    --exclude='*~' \
    --exclude='*.old' \
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
    --exclude='*.ghost_watcher_state' \
    --exclude='ipc-socket' \
    --exclude='lockfile' \
    --exclude='GPUCache' \
    --exclude='CachedImages' \
    --exclude='Crash Reports' \
    --exclude="${STACKS_DIR#/}/jellyfin/config/transcodes" \
    --exclude="${STACKS_DIR#/}/jellyfin/config/cache" \
    --exclude="${STACKS_DIR#/}/jellyfin/config/log" \
    --exclude="${STACKS_DIR#/}/arrs/config/*/*.db-shm" \
    --exclude="${STACKS_DIR#/}/arrs/config/*/*.db-wal" \
    --exclude="${STACKS_DIR#/}/arrs/config/*/MediaCover" \
    --exclude="${STACKS_DIR#/}/arrs/config/*/logs" \
    --exclude="${STACKS_DIR#/}/arrs/config/*/log" \
    --exclude="${STACKS_DIR#/}/arrs/config/*/UpdateLogs" \
    --exclude="${STACKS_DIR#/}/scrutiny/influxdb" \
    --exclude="${STACKS_DIR#/}/audiobookshelf/backups" \
    --exclude="${STACKS_DIR#/}/audiobookshelf/metadata/cache" \
    --exclude="${STACKS_DIR#/}/npm/data/logs" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/apps" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/core" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/lib" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/3rdparty" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/dist" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/resources" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/ocs" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/ocs-provider" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/themes" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/updater" \
    --exclude="${STACKS_DIR#/}/nextcloud/html/version.php" \
    --exclude="${STACKS_DIR#/}/nextcloud/db/\#innodb_redo" \
    --exclude="${STACKS_DIR#/}/nextcloud/db/\#innodb_temp" \
    --exclude="${STACKS_DIR#/}/paperless-ngx/redisdata" \
    --exclude="${STACKS_DIR#/}/pihole/etc-pihole/pihole-FTL.db" \
    --exclude="${STACKS_DIR#/}/pihole/etc-pihole/gravity_old.db" \
    --exclude="${STACKS_DIR#/}/qbittorrent/config/qBittorrent/data/logs" \
    --exclude="${STACKS_DIR#/}/qbittorrent/config/qBittorrent/data/GeoDB" \
    --exclude="${STACKS_DIR#/}/jdownloader/config/logs" \
    --exclude="${STACKS_DIR#/}/jdownloader/config/tmp" \
    -C / \
    "${STACKS_DIR#/}" \
    "${SCRIPTS_DIR#/}" \
    "${CTRL_DIR#/}" \
    "${USER_HOME#/}/.ssh" \
    "${USER_HOME#/}/.local/share/mkcert" \
    etc/ssh \
    tmp/backup-uid.txt \
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
        if [ -f "$stack_dir/compose.yml" ]; then
            echo "  → $(basename "$stack_dir")"
            docker compose -f "$stack_dir/compose.yml" up -d
        fi
    done < "$RUNNING_STACKS_FILE"
else
    echo "⚠️  No snapshot found. Skipping stack startup."
fi

# Validation
if [ $TAR_EXIT_CODE -eq 0 ] || [ $TAR_EXIT_CODE -eq 1 ]; then
    if [ $TAR_EXIT_CODE -eq 1 ]; then
        echo "⚠️  Backup succeeded with warnings (some files changed during read)."
    else
        echo "✅ Backup successful."
    fi
    echo "Verifying backup integrity..."
    if age -d -i "$AGE_KEYFILE" "$BACKUP_DIR/$DOCKER_FILENAME" | zstd -t 2>&1 | \
    sed "s|/\*stdin\*\\\\|$DOCKER_FILENAME|" | \
    awk 'match($0, /([0-9]+) bytes/, a) {
        b = a[1]+0
        if (b >= 2^30) hr = sprintf("%.2f GiB", b/2^30)
        else if (b >= 2^20) hr = sprintf("%.2f MiB", b/2^20)
        else hr = sprintf("%.2f KiB", b/2^10)
        sub(/[0-9]+ bytes/, hr)
    } 1'; then
        echo "✅ Backup verified (decryption + integrity)"
        ls -1t "$BACKUP_DIR"/docker-stacks-*.tar.zst.age \
            | tail -n +3 \
            | xargs -r rm -f
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

# Healthcheck — only check stacks we actually restarted
echo "Waiting for containers to settle..."
MAX_WAIT=120
ELAPSED=0
INTERVAL=10
STUCK=""

while [ $ELAPSED -lt $MAX_WAIT ]; do
    STUCK=""
    if [ -f "$RUNNING_STACKS_FILE" ]; then
        while IFS= read -r stack_dir; do
            if [ -f "$stack_dir/compose.yml" ]; then
                PROBLEMS=$(docker compose -f "$stack_dir/compose.yml" ps -a \
                    --format '{{.Name}} {{.Status}}' 2>/dev/null \
                    | grep -iE "exited|unhealthy|restarting" | awk '{print $1}')
                [ -n "$PROBLEMS" ] && STUCK+="$PROBLEMS"$'\n'
            fi
        done < "$RUNNING_STACKS_FILE"
        STUCK=$(echo "$STUCK" | sed '/^$/d')
    fi

    if [ -z "$STUCK" ]; then
        echo "✅ All containers healthy."
        break
    fi
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

if [ -n "$STUCK" ]; then
    echo "Restarting stuck containers..."
    while IFS= read -r container; do
        [ -n "$container" ] && docker restart "$container"
    done <<< "$STUCK"
fi

echo "🎉 All tasks finished."
exit 0
