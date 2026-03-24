#!/bin/bash
# @DESCRIPTION: Recreates the missing .ocdata marker, user data directories, and appdata_ folders (avatars, theming, previews) that are required for profile pictures and wallpapers to work, then fixes ownership/permissions to www-data and runs occ file scan + repair to bring everything back to a working state.
# @FREQUENCY: Run Once
CONTAINER="nextcloud-app-1"
USERNAME="not-admin"

# Get instance ID
INSTANCE_ID=$(docker exec -u www-data $CONTAINER php occ config:system:get instanceid)
echo "Instance ID: $INSTANCE_ID"

# Create .ocdata
docker exec -u www-data $CONTAINER touch /var/www/html/data/.ocdata

# Create user dirs
docker exec -u www-data $CONTAINER mkdir -p \
  /var/www/html/data/$USERNAME/files \
  /var/www/html/data/$USERNAME/cache \
  /var/www/html/data/$USERNAME/uploads

# Create appdata dirs
docker exec -u www-data $CONTAINER mkdir -p \
  /var/www/html/data/appdata_${INSTANCE_ID}/avatar/$USERNAME \
  /var/www/html/data/appdata_${INSTANCE_ID}/theming/$USERNAME \
  /var/www/html/data/appdata_${INSTANCE_ID}/preview

# Fix permissions
docker exec $CONTAINER chown -R www-data:www-data /var/www/html/data
docker exec $CONTAINER find /var/www/html/data -type d -exec chmod 750 {} \;
docker exec $CONTAINER find /var/www/html/data -type f -exec chmod 640 {} \;

# Scan and repair
docker exec -u www-data $CONTAINER php occ files:scan --all
docker exec -u www-data $CONTAINER php occ files:cleanup
docker exec -u www-data $CONTAINER php occ maintenance:repair
