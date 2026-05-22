#!/bin/bash
# @DESCRIPTION: Watches `/data/assets` + Internal Data, scans Nextcloud via Docker
# @FREQUENCY: Service (Always)

SERVICE_NAME="nc-watcher"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_PATH="$(realpath "$0")"

# ── Self-Install ──────────────────────────────────────────────
if [ "$1" != "--running-as-service" ]; then
    if [ ! -f "$SERVICE_FILE" ]; then
        echo "Installing ${SERVICE_NAME} service..."

        # Increase inotify watchers
        if ! grep -q "fs.inotify.max_user_watches" /etc/sysctl.conf; then
            echo 'fs.inotify.max_user_watches=524288' | sudo tee -a /etc/sysctl.conf
            sudo sysctl -p
        fi

        # Create service file
        cat << EOF | sudo tee "$SERVICE_FILE" > /dev/null
[Unit]
Description=Nextcloud Dynamic Filesystem Watcher
After=network.target docker.service
Requires=docker.service

[Service]
User=root
ExecStart=${SCRIPT_PATH} --running-as-service
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

        sudo systemctl daemon-reload
        sudo systemctl enable --now "${SERVICE_NAME}.service"
        echo "✅ Service installed and started."
        echo "   Verify: sudo journalctl -u ${SERVICE_NAME}.service -f"
        exit 0
    else
        echo "✅ Service already installed. Starting normally."
        sudo systemctl start "${SERVICE_NAME}.service"
        exit 0
    fi
fi

# --- CONFIGURATION -----------------------------------------------------------
NC_USER="not-admin"
# This must match the name you gave the folder in 'External Storage' settings exactly:
NC_MOUNT_NAME="assets"
REAL_ASSETS_DIR="/data/assets"
HOST_DATA_DIR="/data/nextcloud_data"
# Path to your docker-compose file:
COMPOSE_FILE="/opt/stacks/nextcloud/compose.yml"

WATCH_LIST="${HOST_DATA_DIR}/${NC_USER}/files ${REAL_ASSETS_DIR}"
QUEUE_FILE="/tmp/nextcloud_events.log"

trap "pkill -P $$; exit" SIGINT SIGTERM

echo "Starting Nextcloud Docker Watcher..."

# 1. START LISTENER
# We watch the host directories directly
inotifywait -m -r -e close_write -e moved_to -e delete \
    --format '%w' \
    --exclude '/\.' \
    $WATCH_LIST | while read DIR_PATH; do
        echo "$DIR_PATH" >> "$QUEUE_FILE"
    done &

# 2. START PROCESSOR
while true; do
    sleep 10
    if [ -s "$QUEUE_FILE" ]; then
        mv "$QUEUE_FILE" "${QUEUE_FILE}.processing"
        TARGETS=$(sort -u "${QUEUE_FILE}.processing")

        IFS=$'\n'
        for DIR_PATH in $TARGETS; do
            SCAN_PATH=""

            # CASE A: External Assets (/data/assets -> /not-admin/files/Assets)
            if [[ "$DIR_PATH" == "$REAL_ASSETS_DIR"* ]]; then
                # Strip the host path
                RELATIVE=$(echo "$DIR_PATH" | sed "s|^$REAL_ASSETS_DIR||")
                # Map to Nextcloud internal path
                SCAN_PATH="${NC_USER}/files/${NC_MOUNT_NAME}${RELATIVE}"

            # CASE B: Internal Storage (/data/nextcloud_data -> /not-admin/files)
            elif [[ "$DIR_PATH" == "$HOST_DATA_DIR"* ]]; then
                # Strip the host data path
                RELATIVE=$(echo "$DIR_PATH" | sed "s|^$HOST_DATA_DIR||")
                # Map to Nextcloud internal path
                SCAN_PATH="${RELATIVE}" 
            fi

            # Clean trailing slash
            SCAN_PATH=${SCAN_PATH%/}

            # EXECUTE SCAN VIA DOCKER
            if [ ! -z "$SCAN_PATH" ]; then
                echo "[$(date '+%H:%M:%S')] Scanning: $SCAN_PATH"
                # -T disables pseudo-tty (required for background scripts)
                # -u 33 ensures we run as www-data
                docker compose -f "$COMPOSE_FILE" exec -T -u 33 app php occ files:scan --path="$SCAN_PATH"
            fi
        done
        unset IFS
        rm "${QUEUE_FILE}.processing"
    fi
done
