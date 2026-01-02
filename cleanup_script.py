#!/usr/bin/env python3
import os
import glob
import sys # We need the 'sys' module to read arguments

# --- CONFIGURATION ---
# Define the number of recent files you want to keep in each backup folder.
# The script will delete all files older than the N most recent ones.
FILES_TO_KEEP = 2
# --- END OF CONFIGURATION ---


def clean_backup_folder(folder_path, num_to_keep):
    """
    Deletes all but the N most recent files in a specified folder.

    Args:
        folder_path (str): The absolute path to the folder to clean.
        num_to_keep (int): The number of recent files to keep.
    """
    # --- The logic of this function does not need to change at all ---
    try:
        # Get a list of all files in the directory
        files = glob.glob(os.path.join(folder_path, '*'))
        files = [f for f in files if os.path.isfile(f)]

        if len(files) > num_to_keep:
            # Sort files by modification time, newest first
            files.sort(key=os.path.getmtime, reverse=True)
            
            # The files to delete are all files after the ones we want to keep
            files_to_delete = files[num_to_keep:]

            for file_path in files_to_delete:
                try:
                    os.remove(file_path)
                    print(f"Deleted: {file_path}")
                except OSError as e:
                    print(f"Error deleting file {file_path}: {e}")
        else:
            print(f"Skipping '{folder_path}' as it has {num_to_keep} or fewer files.")

    except FileNotFoundError:
        print(f"Error: The folder '{folder_path}' was not found.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    # The script expects folder paths to be provided as arguments.
    # sys.argv is a list of all arguments. sys.argv[0] is the script name itself,
    # so we start from the second item (index 1).
    folders_to_clean = sys.argv[1:]

    if not folders_to_clean:
        print("Error: No backup folders provided. Please provide folder paths as arguments.")
        sys.exit(1) # Exit with an error code

    print("--- Starting Backup Cleanup ---")
    for folder in folders_to_clean:
        # Pass the configured number of files to keep into the function
        clean_backup_folder(folder, FILES_TO_KEEP)
    print("--- Backup Cleanup Finished ---")