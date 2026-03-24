#!/bin/bash
# @DESCRIPTION: Recreates missing markers, user data directories, and appdata_ folders
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

# Create appdata dirs  (FIXED paths)
docker exec -u www-data $CONTAINER mkdir -p \
  /var/www/html/data/appdata_${INSTANCE_ID}/avatar/$USERNAME \
  /var/www/html/data/appdata_${INSTANCE_ID}/theming/images \
  /var/www/html/data/appdata_${INSTANCE_ID}/theming/users/$USERNAME \
  /var/www/html/data/appdata_${INSTANCE_ID}/preview

# Fix permissions
docker exec $CONTAINER chown -R www-data:www-data /var/www/html/data
docker exec $CONTAINER find /var/www/html/data -type d -exec chmod 750 {} \;
docker exec $CONTAINER find /var/www/html/data -type f -exec chmod 640 {} \;

# Scan user files AND appdata separately
docker exec -u www-data $CONTAINER php occ files:scan --all
docker exec -u www-data $CONTAINER php occ files:scan-app-data
docker exec -u www-data $CONTAINER php occ files:cleanup
docker exec -u www-data $CONTAINER php occ maintenance:repair
