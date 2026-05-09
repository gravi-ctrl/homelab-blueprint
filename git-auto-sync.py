#!/usr/bin/env python3
# @DESCRIPTION: Master logic to push/pull Git repos
# @FREQUENCY: Varies
# ==============================================================================
# GENERIC GIT AUTO-SYNC SCRIPT
# Usage: python git_auto_sync.py "/path/to/repo" "Commit Label"
# Example: python git_auto_sync.py "C:\Users\You\stacks" "Server Configs"
# ==============================================================================

import sys
import os
import subprocess
import time
from datetime import datetime

def run_command(command, cwd=None, capture_output=False, suppress_errors=False):
    """
    Helper to run shell commands. 
    Returns a CompletedProcess object.
    """
    stderr_dest = subprocess.DEVNULL if suppress_errors else None
    stdout_dest = subprocess.PIPE if capture_output else None
    
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=stdout_dest,
        stderr=stderr_dest
    )

def main():
    if os.name == 'nt':
        # -q (quiet) stops the banner exchange crash. BatchMode stops interactive prompts.
        os.environ["GIT_SSH_COMMAND"] = "ssh -q -o BatchMode=yes"
        os.environ["GIT_TERMINAL_PROMPT"] = "0"

    # 1. Safety Check: Ensure a path was provided
    if len(sys.argv) < 2:
        print("❌ Error: No directory path provided.")
        print(f"Usage: python {sys.argv[0]} /path/to/repo [Label]")
        sys.exit(1)

    target_dir = sys.argv[1]
    # Default to "Auto-Sync" if no label provided
    label = sys.argv[2] if len(sys.argv) > 2 else "Auto-Sync"

    # 2. Navigate to the folder
    if not os.path.isdir(target_dir):
        print(f"❌ Error: Could not find directory: {target_dir}")
        sys.exit(1)
    
    try:
        os.chdir(target_dir)
    except OSError as e:
        print(f"❌ Error: Could not cd to {target_dir}: {e}")
        sys.exit(1)

    # 3. Stage all changes
    run_command(["git", "add", "."])

    # 4. Commit ONLY if there are changes
    # git diff-index --quiet HEAD returns 0 if no changes, 1 if changes exist
    diff_check = run_command(["git", "diff-index", "--quiet", "HEAD", "--"])
    
    if diff_check.returncode != 0:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        commit_msg = f"{label}: {timestamp}"
        run_command(["git", "commit", "-m", commit_msg])
    else:
        print("Everything up-to-date.")

# 5. Detect current branch and Pull updates
    try:
        branch_proc = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True)
        if branch_proc.returncode != 0:
            print("❌ Error: Not a git repository or no HEAD found.")
            sys.exit(1)
        
        current_branch = branch_proc.stdout.strip()
        
        max_retries = 3
        count = 0
        success = False
        
        while count < max_retries:
            print(f"⬇️  Pulling changes from origin/{current_branch} (Attempt {count+1}/{max_retries})...")
            
            pull_proc = run_command(["git", "pull", "origin", current_branch, "--no-edit", "--rebase", "--autostash"])
            
            if pull_proc.returncode == 0:
                success = True
                break
                
            # BULLETPROOF CONFLICT CHECK
            # Don't guess the exit code. Ask Git directly if it's stuck.
            status_proc = run_command(["git", "status"], capture_output=True)
            status_out = status_proc.stdout.lower()
            
            if "rebase in progress" in status_out or "unmerged paths" in status_out:
                print("❌ Git Conflict detected during rebase.")
                print("⚠️ Attempting to abort stuck rebase to restore clean state...")
                run_command(["git", "rebase", "--abort"], suppress_errors=True)
                sys.exit(1)
            
            # If not a conflict, it is a network error. Trigger the retry!
            print(f"⚠️ Pull failed (Network/SSH Error). Retrying in 10s...")
            time.sleep(10)
            count += 1
            
        if not success:
            print(f"❌ Pull failed after {max_retries} attempts.")
            sys.exit(1)
        
    except Exception as e:
        print(f"❌ Unexpected logic error during pull: {e}")
        sys.exit(1)

    # 6. Push changes with Retry Logic
    max_retries = 3
    count = 0
    success = False

    while count < max_retries:
        # Try standard push. Suppress errors initially (mimicking 2>/dev/null)
        # to handle the "missing upstream" check cleanly.
        push_result = run_command(["git", "push"], suppress_errors=True)

        if push_result.returncode == 0:
            success = True
            break
        
        # Check if the failure was just a missing upstream (First run only)
        if count == 0:
            # Try setting upstream
            upstream_result = run_command(["git", "push", "-u", "origin", "HEAD"], suppress_errors=True)
            if upstream_result.returncode == 0:
                print("✅ Upstream set and pushed successfully.")
                success = True
                break

        print(f"⚠️ Push failed (Attempt {count + 1}/{max_retries}). Retrying in 10s...")
        time.sleep(10)
        count += 1

    if not success:
        print(f"❌ Push Failed after {max_retries} attempts.")
        # Run one last time WITHOUT suppressing errors so the user sees the issue in logs
        run_command(["git", "push"])
        sys.exit(1)

if __name__ == "__main__":
    main()
