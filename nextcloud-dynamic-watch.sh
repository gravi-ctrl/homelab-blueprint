#!/bin/bash

# ==============================================================================
# NEXTCLOUD DYNAMIC WATCHER (Scan Only - BindFS Edition)
# ==============================================================================

# --- CONFIGURATION -----------------------------------------------------------
NC_USER="not-admin"
NC_MOUNT_NAME="assets" # Match your Nextcloud folder name
REAL_ASSETS_DIR="/srv/data/assets"
NC_MAIN_DIR="/mnt/nextcloud_data/data/${NC_USER}/files"
WATCH_LIST="${NC_MAIN_DIR} ${REAL_ASSETS_DIR}"
QUEUE_FILE="/tmp/nextcloud_events.log"

trap "pkill -P $$; exit" SIGINT SIGTERM

echo "Starting Nextcloud Watcher..."

# 1. START LISTENER
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

            # CASE A: External Assets
            if [[ "$DIR_PATH" == "$REAL_ASSETS_DIR"* ]]; then
                RELATIVE=$(echo "$DIR_PATH" | sed "s|^$REAL_ASSETS_DIR||")
                SCAN_PATH="${NC_USER}/files/${NC_MOUNT_NAME}${RELATIVE}"

            # CASE B: Internal Storage
            elif [[ "$DIR_PATH" == "$NC_MAIN_DIR"* ]]; then
                RELATIVE=$(echo "$DIR_PATH" | sed "s|^$NC_MAIN_DIR||")
                SCAN_PATH="${NC_USER}/files${RELATIVE}"
            fi

            # Clean trailing slash
            SCAN_PATH=${SCAN_PATH%/}

            # EXECUTE SCAN (No Permission Fixes needed!)
            if [ ! -z "$SCAN_PATH" ]; then
                echo "[$(date '+%H:%M:%S')] Scanning: $SCAN_PATH"
                nice -n 19 /snap/bin/nextcloud.occ files:scan --path="$SCAN_PATH"
            fi
        done
        unset IFS
        rm "${QUEUE_FILE}.processing"
    fi
done
