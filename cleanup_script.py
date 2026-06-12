#!/usr/bin/env python3
# @DESCRIPTION: Rotates backups by keeping the N most recent items or purging folders entirely. Includes Dry Run safety mode.
# @FREQUENCY: Daily 1am and 1pm
# @USES_ENV: FILES_TO_KEEP
#
# --- USAGE EXAMPLES ---
# 1. Standard (Keep 2 most recent):
#    python3 cleanup_script.py /backup/daily
#
# 2. Delete EVERYTHING in the folder:
#    python3 cleanup_script.py /backup/temp::DELETE_ALL
#
# 3. Dry Run (See what would happen without deleting):
#    DRY_RUN=1 python3 cleanup_script.py /backup/daily

import os
import glob
import sys
import shutil
from dotenv import load_dotenv

script_dir = os.path.dirname(os.path.realpath(__file__))
load_dotenv(os.path.join(script_dir, '.env'))

# --- CONFIGURATION ---
# Defaults to 2 if variable is missing in .env
FILES_TO_KEEP = int(os.environ.get("FILES_TO_KEEP", 2))
DELETE_ALL_TRIGGER = "::DELETE_ALL"

# Safety toggle: set DRY_RUN=1 in shell to test without deleting
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# --- END OF CONFIGURATION ---

def clean_backup_folder(folder_path, num_to_keep):
    """
    Rotates files/folders, keeping only the most recent 'num_to_keep' items.
    Returns True if successful, False if there was an error.
    """
    if not os.path.isdir(folder_path):
        print(f"❌ Error: The folder '{folder_path}' was not found.")
        return False

    try:
        # Get all files and directories
        all_items = glob.glob(os.path.join(folder_path, '*'))

        if len(all_items) > num_to_keep:
            # Sort by modification time (newest first)
            all_items.sort(key=lambda p: max(os.path.getctime(p), os.path.getmtime(p)), reverse=True)
            items_to_delete = all_items[num_to_keep:]

            for item_path in items_to_delete:
                try:
                    if DRY_RUN:
                        print(f"🕵️ [DRY RUN] Would delete: {item_path}")
                        continue

                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        os.remove(item_path)
                        print(f"🗑️ Deleted file: {os.path.basename(item_path)}")
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                        print(f"🗑️ Deleted folder: {os.path.basename(item_path)}")
                except OSError as e:
                    print(f"❌ Error deleting {item_path}: {e}")
                    return False
        else:
            print(f"✅ Skipping '{folder_path}': {len(all_items)} items (limit is {num_to_keep}).")

        return True

    except Exception as e:
        print(f"❌ An unexpected error occurred in {folder_path}: {e}")
        return False


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print("❌ Error: No backup folders provided.")
        sys.exit(1)

    if DRY_RUN:
        print("🧪 --- DRY RUN MODE ACTIVE (No files will be deleted) ---")
    else:
        print("🧹 --- Starting Backup Cleanup ---")

    any_error_occurred = False

    for arg in args:
        if arg.endswith(DELETE_ALL_TRIGGER):
            # Strip the trigger to get real path, set limit to 0
            folder = arg[:-len(DELETE_ALL_TRIGGER)]
            limit = 0
            print(f"📂 Mode: PURGE ALL for '{folder}'")
        else:
            # Standard mode
            folder = arg
            limit = FILES_TO_KEEP
            print(f"📂 Mode: ROTATE (Keep {limit}) for '{folder}'")

        success = clean_backup_folder(folder, limit)
        if not success:
            any_error_occurred = True

    print("✨ --- Cleanup Process Finished ---")

    if any_error_occurred:
        sys.exit(1)
