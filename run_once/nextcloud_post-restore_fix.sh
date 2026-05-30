#!/bin/bash
# @DESCRIPTION: Intelligently fixes permissions, missing markers, user dirs, and runs scans. Safe to run anytime.
# @FREQUENCY: Run Anytime

CONTAINER="nextcloud"
USERNAME="not-admin"

# --- Guard: Is the container even running? ---
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

# --- Missing Data Markers & Scans ---
if ! docker exec -u www-data "$CONTAINER" test -f /var/www/html/data/.ncdata 2>/dev/null; then
    echo "⚠️  .ncdata missing — creating data directory, folders, and running system scans..."

    # Create data directory first
    docker exec -u www-data "$CONTAINER" mkdir -p /var/www/html/data

    # Create .ncdata marker
    docker exec -u www-data "$CONTAINER" bash -c 'echo "# Nextcloud data directory" > /var/www/html/data/.ncdata'

    # Get instance ID
    INSTANCE_ID=$(docker exec -u www-data "$CONTAINER" php occ config:system:get instanceid)
    echo "Instance ID: $INSTANCE_ID"

    # Create user dirs
    docker exec -u www-data "$CONTAINER" mkdir -p \
      /var/www/html/data/"$USERNAME"/files \
      /var/www/html/data/"$USERNAME"/cache \
      /var/www/html/data/"$USERNAME"/uploads

    # Create appdata dirs
    docker exec -u www-data "$CONTAINER" mkdir -p \
      /var/www/html/data/appdata_"${INSTANCE_ID}"/avatar/"$USERNAME" \
      /var/www/html/data/appdata_"${INSTANCE_ID}"/theming/images \
      /var/www/html/data/appdata_"${INSTANCE_ID}"/theming/users/"$USERNAME" \
      /var/www/html/data/appdata_"${INSTANCE_ID}"/preview

    # Ensure permissions for newly created folders
    docker exec "$CONTAINER" chown -R www-data:www-data /var/www/html/data

    echo ">>> Running system scans and maintenance repairs..."
    docker exec -u www-data "$CONTAINER" php occ files:scan --all
    docker exec -u www-data "$CONTAINER" php occ files:scan-app-data
    docker exec -u www-data "$CONTAINER" php occ files:cleanup
    docker exec -u www-data "$CONTAINER" php occ maintenance:repair

    echo "✅ Post-restore setup and scans complete."
else
    echo "✅ .ncdata exists — skipping folder creation and heavy database scans."
fi