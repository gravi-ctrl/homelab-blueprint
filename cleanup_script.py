#!/usr/bin/env python3
# @DESCRIPTION: Cleans folders, keeping the 2 most recent files
# @FREQUENCY: Daily 10am
import os
import glob
import sys

# --- CONFIGURATION ---
FILES_TO_KEEP = 2
# --- END OF CONFIGURATION ---

def clean_backup_folder(folder_path, num_to_keep):
    """
    Returns True if successful, False if there was an error.
    """
    # 1. EXPLICIT CHECK: Fail immediately if folder doesn't exist
    if not os.path.isdir(folder_path):
        print(f"Error: The folder '{folder_path}' was not found.")
        return False  # Signal failure

    try:
        files = glob.glob(os.path.join(folder_path, '*'))
        files = [f for f in files if os.path.isfile(f)]

        if len(files) > num_to_keep:
            files.sort(key=os.path.getmtime, reverse=True)
            files_to_delete = files[num_to_keep:]

            for file_path in files_to_delete:
                try:
                    os.remove(file_path)
                    print(f"Deleted: {file_path}")
                except OSError as e:
                    print(f"Error deleting file {file_path}: {e}")
                    # We don't return False here because some files might have been deleted successfully
        else:
            print(f"Skipping '{folder_path}' as it has {num_to_keep} or fewer files.")
            
        return True # Signal success

    except Exception as e:
        print(f"An unexpected error occurred in {folder_path}: {e}")
        return False # Signal failure


if __name__ == "__main__":
    folders_to_clean = sys.argv[1:]

    if not folders_to_clean:
        print("Error: No backup folders provided.")
        sys.exit(1)

    print("--- Starting Backup Cleanup ---")
    
    # Track if any error occurred
    any_error_occurred = False

    for folder in folders_to_clean:
        # Check the return value of the function
        success = clean_backup_folder(folder, FILES_TO_KEEP)
        if not success:
            any_error_occurred = True

    print("--- Backup Cleanup Finished ---")

    # 2. FINAL EXIT CHECK: If any folder failed, crash the script with Exit Code 1
    if any_error_occurred:
        sys.exit(1)
