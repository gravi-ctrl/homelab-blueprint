#!/bin/bash
# @DESCRIPTION: Recreates missing markers, user data directories, and appdata_ folders. Safe to run anytime — exits gracefully if not needed.
# @FREQUENCY: Run Once

# Updated for Nextcloud 33+
# .ncdata replaced .ocdata and requires specific content
# .ocdata is legacy and no longer needed

CONTAINER="nextcloud"
USERNAME="not-admin"

# --- Guard: Is the container even running? ---
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "⏭️  Nextcloud container not running. Nothing to do."
    exit 0
fi

# --- Guard: Does .ncdata exist? (Means data is probably fine) ---
if docker exec -u www-data "$CONTAINER" test -f /var/www/html/data/.ncdata 2>/dev/null; then
    echo "✅ .ncdata exists — data directory looks intact. Skipping."
    exit 0
fi

echo "⚠️  .ncdata missing — running post-restore fix..."

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

# Fix permissions
docker exec "$CONTAINER" chown -R www-data:www-data /var/www/html/data
docker exec "$CONTAINER" find /var/www/html/data -type d -exec chmod 750 {} \;
docker exec "$CONTAINER" find /var/www/html/data -type f -exec chmod 640 {} \;

# Scan and repair
docker exec -u www-data "$CONTAINER" php occ files:scan --all
docker exec -u www-data "$CONTAINER" php occ files:scan-app-data
docker exec -u www-data "$CONTAINER" php occ files:cleanup
docker exec -u www-data "$CONTAINER" php occ maintenance:repair

echo "✅ Post-restore fix complete."
