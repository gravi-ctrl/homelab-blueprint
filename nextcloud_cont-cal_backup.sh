#!/bin/bash
# @DESCRIPTION: Takes a backup from my contacts and my calendar
# @FREQUENCY: Daily 4am
# 1. Load variables
if [ -f "$HOME/scripts/.nc_backup_env" ]; then
    source "$HOME/scripts/.nc_backup_env"
else
    echo "ERROR: Could not find .env file"
    exit 1
fi

# 2. Setup
CURRENT_DATE=$(date +"%Y-%m-%d")
mkdir -p "$BACKUP_DIR"

# 3. DOWNLOAD CONTACTS
echo "Downloading Contacts..."
wget -q --auth-no-challenge --user="$NC_USER" --password="$NC_PASS" \
"$NC_URL/remote.php/dav/addressbooks/users/$NC_USER/contacts?export" \
-O "$BACKUP_DIR/contacts-backup/contacts_$CURRENT_DATE.vcf"

# DOWNLOAD CALENDAR
# For calendar only The URL is /public-calendars/TOKEN | Create a public url of the calendar, and copy the token to the .nc_backup_env
echo "Downloading Calendar (Public Mode)..."
wget -q "$NC_URL/remote.php/dav/public-calendars/$CALENDAR_TOKEN/?export" \
-O "$BACKUP_DIR/calendar-backup/calendar_$CURRENT_DATE.ics"

echo "Done."
