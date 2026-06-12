#!/bin/bash
# @DESCRIPTION: Watches `/data/assets` + Internal Data, scans Nextcloud via Docker
# @FREQUENCY: Service (Always)
# @USES_ENV: NEXTCLOUD_USER, NEXTCLOUD_DATA_DIR, NEXTCLOUD_CONTAINER, NEXTCLOUD_MOUNT_NAME, DATA_DIR

[[ -f "/opt/scripts/.env" ]] || { echo ".env does not exist at /opt/scripts" >&2; exit 1; }
source "/opt/scripts/.env"

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
NC_USER="${NEXTCLOUD_USER}"
# This must match the name you gave the folder in 'External Storage' settings exactly:
NC_MOUNT_NAME="${NEXTCLOUD_MOUNT_NAME}"
REAL_ASSETS_DIR="${DATA_DIR}"
HOST_DATA_DIR="${NEXTCLOUD_DATA_DIR}"
CONTAINER_NAME="${NEXTCLOUD_CONTAINER}"

QUEUE_FILE="/tmp/nextcloud_events.log"

trap "pkill -P $$; exit" SIGINT SIGTERM

echo "Starting Nextcloud Docker Watcher..."

# 1. START LISTENER
WATCH_LIST=("${HOST_DATA_DIR}/${NC_USER}/files" "${REAL_ASSETS_DIR}")

inotifywait -m -r -e close_write -e moved_to -e delete \
    --format '%w%f' \
    --exclude '/\.' \
    "${WATCH_LIST[@]}" | while IFS= read -r DIR_PATH; do
        echo "$DIR_PATH" >> "$QUEUE_FILE"
    done &

# 2. START PROCESSOR
while true; do
    sleep 10
    if [ -s "$QUEUE_FILE" ]; then
        mv "$QUEUE_FILE" "${QUEUE_FILE}.processing"
        touch "$QUEUE_FILE"

        IFS=$'\n'
        for DIR_PATH in $(sort -u "${QUEUE_FILE}.processing"); do
            SCAN_PATH=""

            # CASE A: External Assets
            if [[ "$DIR_PATH" == "$REAL_ASSETS_DIR"* ]]; then
                RELATIVE=$(echo "$DIR_PATH" | sed "s|^$REAL_ASSETS_DIR||")
                SCAN_PATH="${NC_USER}/files/${NC_MOUNT_NAME}${RELATIVE}"

            # CASE B: Internal Storage
            elif [[ "$DIR_PATH" == "$HOST_DATA_DIR"* ]]; then
                RELATIVE=$(echo "$DIR_PATH" | sed "s|^$HOST_DATA_DIR||")
                SCAN_PATH="${RELATIVE}"
            fi

            SCAN_PATH="${SCAN_PATH%/}"
            [ -z "$SCAN_PATH" ] && continue

            # Always collapse to parent directory — one scan covers all siblings
            SCAN_PATH=$(dirname "$SCAN_PATH")

            echo "$SCAN_PATH" >> "${QUEUE_FILE}.targets"
        done
        unset IFS

        # 3. EXECUTE DEDUPLICATED SCANS
        if [ -f "${QUEUE_FILE}.targets" ]; then
            IFS=$'\n'
            for SCAN_PATH in $(sort -u "${QUEUE_FILE}.targets"); do
                echo "[$(date '+%H:%M:%S')] Scanning: $SCAN_PATH"
                docker exec -u 33 "$CONTAINER_NAME" php occ files:scan --path="$SCAN_PATH"
            done
            unset IFS

            rm "${QUEUE_FILE}.targets"
        fi

        rm "${QUEUE_FILE}.processing"
    fi
done
