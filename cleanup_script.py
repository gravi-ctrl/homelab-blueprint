#!/usr/bin/env python3
# @DESCRIPTION: Cleans folders (files AND subdirs), keeping recent or deleting all
# @FREQUENCY: Daily 1am and 1pm
#
# --- USAGE EXAMPLES ---
# 1. Standard (Keep 2 most recent):
#    /home/user/scripts/cleanup_script.py /srv/backup/daily
#
# 2. Delete EVERYTHING in the folder:
#    /home/user/scripts/cleanup_script.py /srv/backup/temp::DELETE_ALL
#
# 3. Mixed (Keep 2 in 'daily', delete all in 'temp'):
#    /home/user/scripts/cleanup_script.py /srv/backup/daily /srv/backup/temp::DELETE_ALL

import os
import glob
import sys
import shutil

# --- CONFIGURATION ---
FILES_TO_KEEP = 2
DELETE_ALL_TRIGGER = "::DELETE_ALL"
# --- END OF CONFIGURATION ---

def clean_backup_folder(folder_path, num_to_keep):
    """
    Returns True if successful, False if there was an error.
    """
    if not os.path.isdir(folder_path):
        print(f"Error: The folder '{folder_path}' was not found.")
        return False

    try:
        # Get all files and directories
        all_items = glob.glob(os.path.join(folder_path, '*'))
        
        # Sort by modification time (newest first)
        if len(all_items) > num_to_keep:
            all_items.sort(key=os.path.getmtime, reverse=True)
            items_to_delete = all_items[num_to_keep:]

            for item_path in items_to_delete:
                try:
                    if os.path.isfile(item_path) or os.path.islink(item_path):
                        os.remove(item_path)
                        print(f"Deleted file: {item_path}")
                    elif os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                        print(f"Deleted folder: {item_path}")
                except OSError as e:
                    print(f"Error deleting {item_path}: {e}")
        else:
            print(f"Skipping '{folder_path}' as it has {num_to_keep} or fewer items.")

        return True

    except Exception as e:
        print(f"An unexpected error occurred in {folder_path}: {e}")
        return False


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        print("Error: No backup folders provided.")
        sys.exit(1)

    print("--- Starting Backup Cleanup ---")

    any_error_occurred = False

    for arg in args:
        if arg.endswith(DELETE_ALL_TRIGGER):
            # Strip the trigger to get real path, set limit to 0
            folder = arg[:-len(DELETE_ALL_TRIGGER)]
            limit = 0
            print(f"Mode: DELETE ALL for '{folder}'")
        else:
            # Standard mode
            folder = arg
            limit = FILES_TO_KEEP

        success = clean_backup_folder(folder, limit)
        if not success:
            any_error_occurred = True

    print("--- Backup Cleanup Finished ---")

    if any_error_occurred:
        sys.exit(1)
