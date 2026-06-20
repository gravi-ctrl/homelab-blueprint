#!/usr/bin/env python3
# @DESCRIPTION: Master logic to push/pull Git repos (all local branches)
# @FREQUENCY: Varies
# @CRON: user
# ==============================================================================
# GENERIC GIT AUTO-SYNC SCRIPT
# Usage: python git_auto_sync.py "/path/to/repo" "Commit Label"
# Example: python git_auto_sync.py "C:\Users\You\stacks" "Server Configs"
#
# Syncs EVERY local branch: for each branch -> checkout, pull/rebase with
# retry, push with retry. A failure on one branch does not stop the others;
# a summary is printed at the end and the script exits non-zero if any
# branch failed.
# ==============================================================================

import sys
import os
import subprocess
import time
import socket
import random  # Added for randomized network jitter
from datetime import datetime

# ── Force UTF-8 console output
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ── Cross-platform repo lock (same OS-conditional pattern as cron-guard.py's
#    process-tree kill: branch once on sys.platform, identical call sites)
if sys.platform == "win32":
    import msvcrt
    def _try_lock(fd):
        try:
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    def _unlock(fd):
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl
    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

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

def get_local_branches():
    """
    Returns a list of local branch names (excludes detached HEAD entries).
    """
    proc = run_command(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True
    )
    if proc.returncode != 0 or not proc.stdout:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

def sync_branch(branch_name, max_retries=3):
    """
    Checks out, pulls/rebases, and pushes a single branch.
    Returns (success: bool, reason: str) — reason is set on failure for the summary.
    """
    print(f"\n=== Branch: {branch_name} ===")

    checkout_proc = run_command(["git", "checkout", branch_name], capture_output=True)
    if checkout_proc.returncode != 0:
        msg = (checkout_proc.stderr or "Unknown checkout error").strip()
        print(f"❌ Could not check out '{branch_name}': {msg}")
        return False, f"checkout failed: {msg}"

    # ── Pull / Rebase with retry ─────────────────────────────────────────
    count = 0
    pulled = False

    while count < max_retries:
        print(f"⬇️  Pulling changes from origin/{branch_name} (Attempt {count+1}/{max_retries})...")

        pull_proc = run_command(["git", "pull", "origin", branch_name, "--no-edit", "--rebase", "--autostash"], capture_output=True)

        if pull_proc.returncode == 0:
            print(pull_proc.stdout.strip() if pull_proc.stdout else "✅ Pull successful.")
            pulled = True
            break

        err_low = pull_proc.stderr.lower() if pull_proc.stderr else ""

        # Branch has never been pushed before. Nothing to pull yet.
        if "couldn't find remote ref" in err_low or "no tracking information" in err_low:
            print(f"ℹ️  No remote branch 'origin/{branch_name}' yet — nothing to pull. Will create it on push.")
            pulled = True
            break

        # Conflict Check
        status_proc = run_command(["git", "status"], capture_output=True)
        status_out = status_proc.stdout.lower() if status_proc.stdout else ""
        if "rebase in progress" in status_out or "unmerged paths" in status_out or "conflict" in err_low:
            print(f"❌ Git Conflict detected on '{branch_name}'. Manual merge required.")
            print("⚠️ Attempting to abort stuck rebase to restore clean state...")
            run_command(["git", "rebase", "--abort"], suppress_errors=True)
            return False, "merge conflict during pull/rebase"

        # Auth/Permission Check
        if "permission denied" in err_low or "authentication failed" in err_low:
            print(f"❌ CRITICAL: Authentication/Permission error on '{branch_name}'. Check your SSH keys/token.")
            return False, "authentication error during pull"

        # Repo missing / Network hiccup
        if "could not read from remote" in err_low or "not found" in err_low:
            print("⚠️ Warning: Remote unreachable. (Might be a transient network/SSH drop)")

        # Otherwise treat as a network/server error and retry with jitter
        jitter_sleep = random.randint(25, 35)
        print(f"⚠️ Pull failed (Network/Server Error). Retrying in {jitter_sleep}s...")
        time.sleep(jitter_sleep)
        count += 1

    if not pulled:
        print(f"❌ Pull failed after {max_retries} attempts on '{branch_name}'.")
        return False, "pull failed after max retries"

    # ── Push with retry ───────────────────────────────────────────────────
    count = 0
    pushed = False
    last_push_err = "Unknown Error"

    while count < max_retries:
        print(f"🚀 Pushing '{branch_name}' to origin (Attempt {count + 1}/{max_retries})...")

        push_result = run_command(["git", "push", "origin", branch_name], capture_output=True)

        if push_result.returncode == 0:
            if push_result.stdout:
                print(push_result.stdout.strip())
            if push_result.stderr:
                print(push_result.stderr.strip())
            pushed = True
            break

        err_low = push_result.stderr.lower() if push_result.stderr else ""
        raw_error = push_result.stderr.strip() if push_result.stderr else "Unknown Error"
        last_push_err = raw_error

        # History Rewritten / Diverged (Non-fast-forward / Behind)
        if "rejected" in err_low and ("fetch first" in err_low or "use 'git pull'" in err_low or "non-fast-forward" in err_low):
            print(f"⚠️ Push rejected on '{branch_name}' — remote is ahead. Pulling and retrying...")
            retry_pull = run_command(["git", "pull", "origin", branch_name, "--no-edit", "--rebase", "--autostash"], capture_output=True)
            if retry_pull.returncode != 0:
                retry_err_low = retry_pull.stderr.lower() if retry_pull.stderr else ""
                status_proc = run_command(["git", "status"], capture_output=True)
                status_out = status_proc.stdout.lower() if status_proc.stdout else ""
                if "rebase in progress" in status_out or "unmerged paths" in status_out or "conflict" in retry_err_low:
                    print(f"❌ Git Conflict detected on '{branch_name}' during push-retry pull.")
                    run_command(["git", "rebase", "--abort"], suppress_errors=True)
                    return False, "merge conflict during push-triggered pull"
            count += 1
            continue

        # File too large (GitHub/Codeberg limit)
        if "this is larger than github's recommended maximum file size" in err_low or "gh001" in err_low:
            print(f"❌ CRITICAL: Push rejected on '{branch_name}' because a file is too large.")
            return False, "file too large"

        # Auth/Permission
        if "permission denied" in err_low or "authentication failed" in err_low:
            print(f"❌ CRITICAL: Authentication error during push on '{branch_name}'.")
            return False, "authentication error during push"

        # Missing upstream
        if "upstream" in err_low or "set-upstream" in err_low:
            upstream_result = run_command(["git", "push", "-u", "origin", branch_name], capture_output=True)
            if upstream_result.returncode == 0:
                print(f"✅ Upstream set and pushed successfully for '{branch_name}'.")
                pushed = True
                break

        # Generic failure (Network, Timeout, Server 500) — retry with jitter
        jitter_sleep = random.randint(25, 35)
        print(f"⚠️ Push failed on '{branch_name}'. Error: {raw_error}")
        print(f"Waiting {jitter_sleep}s before retry...")
        time.sleep(jitter_sleep)
        count += 1

    if not pushed:
        print(f"❌ Push failed after {max_retries} attempts on '{branch_name}'.")
        return False, f"push failed after max retries: {last_push_err}"

    return True, ""

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

    # Lock this repo so gitops-deploy.sh (or any other git-touching script)
    # can't operate on it at the same time. Non-blocking: if busy, skip this
    # run entirely rather than risk two processes mutating the same checkout.
    git_dir = os.path.join(target_dir, ".git")
    if not os.path.isdir(git_dir):
        print(f"❌ Error: {target_dir} is not a git repository (no .git directory).")
        sys.exit(1)

    lock_fd = open(os.path.join(git_dir, "sync.lock"), "w")
    if not _try_lock(lock_fd):
        print(f"⏭️  {target_dir} is locked by another process. Skipping this run.")
        lock_fd.close()
        sys.exit(0)

    try:
        # Remember the branch we started on, to restore it at the end
        starting_branch_proc = run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True)
        starting_branch = starting_branch_proc.stdout.strip() if starting_branch_proc.returncode == 0 else None

        # 3. Stage all changes (on whatever branch is currently checked out)
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

        # 5. Discover all local branches
        branches = get_local_branches()
        if not branches:
            print("❌ Error: No local branches found (not a git repository?).")
            sys.exit(1)

        print(f"\n📋 Found {len(branches)} local branch(es): {', '.join(branches)}")

        # 6. Sync each branch independently; keep going even if one fails
        results = {}
        for branch in branches:
            ok, reason = sync_branch(branch)
            results[branch] = (ok, reason)

        # 7. Restore the original branch
        if starting_branch:
            run_command(["git", "checkout", starting_branch], capture_output=True, suppress_errors=True)

        # 8. Summary
        print("\n--- Sync Summary ---")
        any_failed = False
        for branch, (ok, reason) in results.items():
            if ok:
                print(f"✅ {branch}: synced successfully")
            else:
                any_failed = True
                print(f"❌ {branch}: FAILED — {reason}")

        if any_failed:
            sys.exit(1)
    finally:
        _unlock(lock_fd)
        lock_fd.close()

if __name__ == "__main__":
    main()
