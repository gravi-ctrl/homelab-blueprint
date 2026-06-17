#!/bin/bash
# @DESCRIPTION: Intelligently fixes permissions, missing markers, user dirs, and runs scans. Safe to run anytime.
# @FREQUENCY: Run Once (triggered by `container-watcher. sh`)
# @USES_ENV: NEXTCLOUD_USER, NEXTCLOUD_CONTAINER

[[ -f "/opt/scripts/.env" ]] || { echo ".env does not exist at /opt/scripts" >&2; exit 1; }
source "/opt/scripts/.env"

CONTAINER="${NEXTCLOUD_CONTAINER}"
USERNAME="${NEXTCLOUD_USER}"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "⏭️  Nextcloud container not running. Nothing to do."
    exit 0
fi

# --- Fix Permissions ONLY if they are broken ---
CURRENT_OWNER=$(docker exec "$CONTAINER" stat -c '%U' /var/www/html)
DATA_OWNER=$(docker exec "$CONTAINER" stat -c '%U' /var/www/html/data 2>/dev/null || echo "missing")

if [ "$CURRENT_OWNER" != "www-data" ] || [ "$DATA_OWNER" != "www-data" ]; then
    echo "⚠️  Wrong permissions detected (html: $CURRENT_OWNER, data: $DATA_OWNER). Fixing ownership..."
    docker exec "$CONTAINER" chown -R www-data:www-data /var/www/html
    docker exec "$CONTAINER" find /var/www/html/data -type d -exec chmod 750 {} + 2>/dev/null || true
    docker exec "$CONTAINER" find /var/www/html/data -type f -exec chmod 640 {} + 2>/dev/null || true
else
    echo "✅ Permissions on /var/www/html and /var/www/html/data look correct (www-data)."
fi

# --- Check health indicators ---
NCDATA_EXISTS=false
USER_DIR_EXISTS=false

docker exec -u www-data "$CONTAINER" test -f /var/www/html/data/.ncdata 2>/dev/null \
    && NCDATA_EXISTS=true

docker exec -u www-data "$CONTAINER" test -d /var/www/html/data/"$USERNAME" 2>/dev/null \
    && USER_DIR_EXISTS=true

# --- Fresh install ---
if [ "$NCDATA_EXISTS" = false ] || [ "$USER_DIR_EXISTS" = false ]; then
    echo "⚠️  Fresh setup needed — creating directories and running scans..."

    # Create data directory and .ncdata marker
    docker exec -u www-data "$CONTAINER" mkdir -p /var/www/html/data
    docker exec -u www-data "$CONTAINER" bash -c 'echo "# Nextcloud data directory" > /var/www/html/data/.ncdata'

    # Get instance ID
    INSTANCE_ID=$(docker exec -u www-data "$CONTAINER" php occ config:system:get instanceid)
    echo "Instance ID: $INSTANCE_ID"

    # Create user dirs
    docker exec -u www-data "$CONTAINER" mkdir -p \
        /var/www/html/data/"$USERNAME"/files/Voice_Memos \
        /var/www/html/data/"$USERNAME"/cache \
        /var/www/html/data/"$USERNAME"/uploads

    # Create appdata dirs
    docker exec -u www-data "$CONTAINER" mkdir -p \
        /var/www/html/data/appdata_"${INSTANCE_ID}"/avatar/"$USERNAME" \
        /var/www/html/data/appdata_"${INSTANCE_ID}"/theming/images \
        /var/www/html/data/appdata_"${INSTANCE_ID}"/theming/users/"$USERNAME" \
        /var/www/html/data/appdata_"${INSTANCE_ID}"/theming/global \
        /var/www/html/data/appdata_"${INSTANCE_ID}"/js/core \
        /var/www/html/data/appdata_"${INSTANCE_ID}"/preview

    # Fix permissions
    docker exec -u www-data "$CONTAINER" php occ maintenance:mode --on
    docker exec "$CONTAINER" chown -R www-data:www-data /var/www/html/data
    docker exec -u www-data "$CONTAINER" php occ maintenance:mode --off

    echo ">>> Running system scans and maintenance repairs..."
    docker exec -u www-data "$CONTAINER" php occ files:scan --all
    docker exec -u www-data "$CONTAINER" php occ files:scan-app-data
    docker exec -u www-data "$CONTAINER" php occ files:cleanup
    docker exec -u www-data "$CONTAINER" php occ maintenance:repair

    echo "✅ Post-restore setup and scans complete."

# --- Healthy install ---
else
    echo "✅ Nextcloud looks healthy (.ncdata: $NCDATA_EXISTS, user dir: $USER_DIR_EXISTS)."
    echo ">>> Running maintenance:repair as a precaution..."
    docker exec -u www-data "$CONTAINER" php occ maintenance:repair
    echo "✅ Done."
fi
