#!/bin/bash

# ==============================================================================
# SCRIPT: Nextcloud Auto-Backup (Contacts & Calendar)
# @DESCRIPTION: Backs up all contacts (via Auth) and the personal calendar (via Public Link) to .vcf and .ics files.
# @FREQUENCY: Daily 4am
#
# RECOVERY INSTRUCTIONS:
#   1. Create file: $HOME/scripts/.nc_backup_env
#   2. Add the following content (fill in the secrets):
#      -------------------------------------------------------------------------
#      NC_URL="http://192.168.1.xxx:8080"
#      NC_USER="your-username"
#      NC_PASS="your-app-password"   # Create in NC Settings -> Security
#      BACKUP_DIR="/srv/data/assets/syncthing/Backup"
#      CALENDAR_TOKEN="Y8eR7z..."    # Create Public Link -> Copy code at end of URL
#      -------------------------------------------------------------------------
# ==============================================================================

# 1. FAIL-SAFE CONFIGURATION
# ------------------------------------------------------------------------------
# set -e: Exit immediately if any command fails (triggers your Cron/Telegram alert)
set -e

# 2. LOAD CONFIGURATION
# ------------------------------------------------------------------------------
ENV_FILE="$HOME/scripts/.nc_backup_env"

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "ERROR: Configuration file not found at: $ENV_FILE"
    echo "Please read the header of this script for recovery instructions."
    exit 1
fi

# Sanity Check: Ensure critical variables are loaded
if [[ -z "$NC_USER" || -z "$CALENDAR_TOKEN" ]]; then
    echo "ERROR: Variables NC_USER or CALENDAR_TOKEN are missing in .env file."
    exit 1
fi

# 3. PREPARE DIRECTORIES
# ------------------------------------------------------------------------------
CURRENT_DATE=$(date +"%Y-%m-%d")

# Create main and sub-directories (mkdir -p ignores error if they exist)
mkdir -p "$BACKUP_DIR/contacts-backup"
mkdir -p "$BACKUP_DIR/calendar-backup"

# 4. EXECUTE BACKUPS
# ------------------------------------------------------------------------------

# --- A. CONTACTS (Authenticated Method) ---
echo "[1/2] Downloading Contacts for user: $NC_USER..."
wget -q --auth-no-challenge --user="$NC_USER" --password="$NC_PASS" \
     "$NC_URL/remote.php/dav/addressbooks/users/$NC_USER/contacts?export" \
     -O "$BACKUP_DIR/contacts-backup/contacts_$CURRENT_DATE.vcf"

# --- B. CALENDAR (Public Token Method) ---
# Note: We use the Public Link method to avoid issues with Internal UUIDs.
echo "[2/2] Downloading Calendar (Public Token: ${CALENDAR_TOKEN:0:4}***)..."
wget -q "$NC_URL/remote.php/dav/public-calendars/$CALENDAR_TOKEN/?export" \
     -O "$BACKUP_DIR/calendar-backup/calendar_$CURRENT_DATE.ics"

# 5. COMPLETION
# ------------------------------------------------------------------------------
echo "SUCCESS: Backup completed at $(date)"
# Script ends with exit code 0
