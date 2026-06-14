#!/usr/bin/env python3
# @DESCRIPTION: Master logic to push/pull Git repos
# @FREQUENCY: Varies
# @CRON: user
# ==============================================================================
# GENERIC GIT AUTO-SYNC SCRIPT
# Usage: python git_auto_sync.py "/path/to/repo" "Commit Label"
# Example: python git_auto_sync.py "C:\Users\You\stacks" "Server Configs"
# ==============================================================================

import sys
import os
import subprocess
import time
import socket
import random  # Added for randomized network jitter
from datetime import datetime

def run_command(command, cwd=None, capture_output=False, suppress_errors=False):
    """
    Helper to run shell commands. 
    Returns a CompletedProcess object.
    """
    if capture_output:
        stdout_dest = subprocess.PIPE
        stderr_dest = subprocess.DEVNULL if suppress_errors else subprocess.PIPE
    else:
        stdout_dest = None
        stderr_dest = subprocess.DEVNULL if suppress_errors else None
    
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=stdout_dest,
        stderr=stderr_dest
    )

def main():
    # ── UNIVERSAL AUTOMATION SAFEGUARDS (Cross-Platform) ──────────────────
    # BatchMode: Stops SSH from asking questions/prompts on all systems.
    # StrictHostKeyChecking=accept-new: Quietly registers Codeberg/GitHub keys on first run, but prevents hangs.
    # GIT_TERMINAL_PROMPT=0: Prevents Git from prompting for usernames/passwords.
    ssh_opts = "-q -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
    os.environ["GIT_SSH_COMMAND"] = f"ssh {ssh_opts}"
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
        
    hostname = socket.gethostname()
    os.environ["GIT_AUTHOR_NAME"] = "AutoSync Bot"
    os.environ["GIT_AUTHOR_EMAIL"] = f"auto-sync@{hostname}.local"
    os.environ["GIT_COMMITTER_NAME"] = "AutoSync Bot"
    os.environ["GIT_COMMITTER_EMAIL"] = f"auto-sync@{hostname}.local"

    # 1. Safety Check: Ensure a path was provided
    if len(sys.argv) < 2:
        print("❌ Error: No directory path provided.")
        print(f"Usage: python {sys.argv[0]} /path/to/repo [Label]")
        sys.exit(1)

    # Normalize paths to be cross-platform safe (handles \ and / automatically)
    target_dir = os.path.normpath(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else "Auto-Sync"

    print(f"\n--- Processing: {label} ({target_dir}) ---")
  
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
    head_check = run_command(["git", "rev-parse", "--verify", "HEAD"], capture_output=True, suppress_errors=True)
    fresh_repo = head_check.returncode != 0
    diff_check = run_command(["git", "diff-index", "--quiet", "HEAD", "--"]) if not fresh_repo else None

    if fresh_repo or (diff_check and diff_check.returncode != 0):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_command(["git", "commit", "-m", f"{label}: {timestamp}"])
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
            
            pull_proc = run_command(["git", "pull", "origin", current_branch, "--no-edit", "--rebase", "--autostash"], capture_output=True)
            
            if pull_proc.returncode == 0:
                print(pull_proc.stdout.strip() if pull_proc.stdout else "✅ Pull successful.")
                success = True
                break
                
            err_low = pull_proc.stderr.lower() if pull_proc.stderr else ""
            
            # 5a. Conflict Check
            status_proc = run_command(["git", "status"], capture_output=True)
            status_out = status_proc.stdout.lower() if status_proc.stdout else ""
            if "rebase in progress" in status_out or "unmerged paths" in status_out or "conflict" in err_low:
                print("❌ Git Conflict detected. Manual merge required.")
                print("⚠️ Attempting to abort stuck rebase to restore clean state...")
                run_command(["git", "rebase", "--abort"], suppress_errors=True)
                sys.exit(1)
            
            # 5b. Auth/Permission Check
            if "permission denied" in err_low or "authentication failed" in err_low:
                print("❌ CRITICAL: Authentication/Permission error. Check your SSH keys/token.")
                sys.exit(1)

            # 5c. Repo missing / Network hiccup (RESTORED WARNING BLOCK)
            if "could not read from remote" in err_low or "not found" in err_low:
                print("⚠️ Warning: Remote unreachable. (Might be a transient network/SSH drop)")

            # If not a critical error, it is a network error. Trigger the retry with Jitter!
            jitter_sleep = random.randint(25, 35)
            print(f"⚠️ Pull failed (Network/Server Error). Retrying in {jitter_sleep}s...")
            time.sleep(jitter_sleep)
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
        print(f"🚀 Pushing updates to all remotes (Attempt {count + 1}/{max_retries})...")
        
        push_result = run_command(["git", "push"], capture_output=True)

        if push_result.returncode == 0:
            if push_result.stdout:
                print(push_result.stdout.strip())
            if push_result.stderr:
                print(push_result.stderr.strip())
            success = True
            break

        err_low = push_result.stderr.lower() if push_result.stderr else ""
        raw_error = push_result.stderr.strip() if push_result.stderr else "Unknown Error"

        # 6a. History Rewritten / Diverged (Non-fast-forward / Behind)
        if "rejected" in err_low and ("fetch first" in err_low or "use 'git pull'" in err_low or "non-fast-forward" in err_low):
            print("⚠️ Push rejected — remote is ahead. Pulling and retrying...")
            run_command(["git", "pull", "origin", current_branch, "--no-edit", "--rebase", "--autostash"])
            count += 1
            continue

        # 6b. File too large (GitHub/Codeberg limit)
        if "this is larger than github's recommended maximum file size" in err_low or "gh001" in err_low:
            print("❌ CRITICAL: Push rejected because a file is too large.")
            sys.exit(1)

        # 6c. Auth/Permission
        if "permission denied" in err_low or "authentication failed" in err_low:
            print("❌ CRITICAL: Authentication error during push.")
            sys.exit(1)

        # 6d. Check if the failure was just a missing upstream
        if "upstream" in err_low or "set-upstream" in err_low:
            upstream_result = run_command(["git", "push", "-u", "origin", "HEAD"], capture_output=True)
            if upstream_result.returncode == 0:
                print("✅ Upstream set and pushed successfully.")
                success = True
                break

        # If we reached here, it is a generic failure (Network, Timeout, Server 500)
        # Apply random jitter to sleep duration
        jitter_sleep = random.randint(25, 35)
        print(f"⚠️ Push failed. Error: {raw_error}")
        print(f"Waiting {jitter_sleep}s before retry...")
        time.sleep(jitter_sleep)
        count += 1

    if not success:
        print(f"❌ Push Failed after {max_retries} attempts.")
        if push_result.stderr:
            print(push_result.stderr.strip())
        sys.exit(1)

if __name__ == "__main__":
    main()
