#!/usr/bin/env python3
# @DESCRIPTION: Master logic to push/pull Git repos (all local branches)
# @FREQUENCY: Varies
# @CRON: user
# @CAPABILITIES:
#   • Isolates branch syncs in worktrees to protect live Docker deployment directories.
#   • Prevents concurrent execution via cross-platform file locking, eliminating race conditions.
#   • Alarms only on unique failures, silencing repeated alerts to prevent notification fatigue.
#   • Aborts commits if rebases, detached HEADs, or excessive file deletions are detected.
#   • Retries network operations with randomized jitter, aborting immediately on auth failures.
#   • Continues syncing remaining branches after individual failures, ensuring partial recovery.
# ==============================================================================
# GENERIC GIT AUTO-SYNC SCRIPT
# Usage: python git_auto_sync.py "/path/to/repo" "Commit Label"
# Example: python git_auto_sync.py "C:\Users\You\stacks" "Server Configs"
#
# Syncs EVERY local branch WITHOUT ever switching the working tree:
#   * the checked-out branch  -> commit, pull --rebase, push (in place)
#   * every other branch      -> fetch, then fast-forward / push / rebase in a
#                                throwaway worktree. The live directory is never
#                                touched, so a deploy dir full of compose files
#                                can't be swapped out from under Docker.
# A failure on one branch does not stop the others; a summary is printed at the
# end and the script exits non-zero if any branch failed.
#
# ALARM-ONCE: a problem makes the script exit 1 (so cron-guard alarms) only the FIRST time.
# While the same problem persists, later runs still log it but exit 0 (silent). The state is
# kept in .git/autosync-alarm.json and clears itself as soon as a run finishes with no
# problems. A DIFFERENT problem alarms again.
#
# SAFETY GATES (all abort BEFORE `git add .`):
#   * rebase/merge in progress or detached HEAD
#   * checked-out branch != expected branch (AUTOSYNC_BRANCH, else origin/HEAD,
#     else main/master). Optional: AUTOSYNC_REQUIRE_GITIGNORE=1 also demands a .gitignore.
#   * more than AUTOSYNC_MAX_DELETIONS (default 3) tracked files would be
#     deleted by the commit (override once with AUTOSYNC_ALLOW_DELETIONS=1)
# ==============================================================================

import hashlib
import json
import os
import random  # Added for randomized network jitter
import shutil
import socket
import subprocess
import sys
import tempfile
import time
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

def sync_checked_out_branch(branch_name, max_retries=3):
    """
    Pulls/rebases and pushes the branch that is ALREADY checked out.
    Never switches branches. Returns (success: bool, reason: str).
    """
    print(f"\n=== Branch: {branch_name} (checked out) ===")

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

class Blocked(Exception):
    """A safety gate refused to run. `signature` identifies the problem for alarm-once dedup."""
    def __init__(self, signature, message):
        super().__init__(message)
        self.signature = signature
        self.message = message

ALARM_FILE = os.path.join(".git", "autosync-alarm.json")  # cwd is the repo root

def _load_alarmed():
    try:
        with open(ALARM_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()

def _store_alarmed(signatures):
    try:
        if not signatures:
            if os.path.exists(ALARM_FILE):
                os.remove(ALARM_FILE)
            return
        with open(ALARM_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(signatures), f)
    except OSError:
        pass

def finalize_alarms(problems):
    """
    problems: {signature: message}. Returns the process exit code.
      no problems          -> clear state, exit 0
      a NEW problem        -> remember it, exit 1 (cron-guard alarms)
      only KNOWN problems  -> stay silent: log it, exit 0
    """
    alarmed = _load_alarmed()
    _store_alarmed(set(problems))
    if not problems:
        return 0
    if any(sig not in alarmed for sig in problems):
        print("\n🚨 New problem(s) detected. Alarming once; later runs stay silent until this is fixed:")
        for sig, msg in problems.items():
            print(f"   - {msg}")
        return 1
    print("\n🔕 Known problem(s) unchanged since the last alarm; staying silent until fixed:")
    for sig, msg in problems.items():
        print(f"   - {msg}")
    return 0

def _git_out(args, cwd=None):
    p = subprocess.run(["git"] + args, cwd=cwd, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.strip() if p.returncode == 0 else ""

def _is_ancestor(a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def _checked_out_elsewhere(branch_name):
    """True if another worktree (e.g. generate-dashboards.sh's temp one) has this branch."""
    out = _git_out(["worktree", "list", "--porcelain"])
    return f"branch refs/heads/{branch_name}" in out.splitlines()

def expected_branch(current):
    env = os.environ.get("AUTOSYNC_BRANCH")
    if env:
        return env
    head = _git_out(["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"])
    if head.startswith("origin/"):
        return head[len("origin/"):]
    local = get_local_branches()
    for cand in ("main", "master"):
        if cand in local:
            return cand
    return current

def preflight_checks():
    """Raise Blocked rather than commit from a state that isn't trustworthy. Returns current branch."""
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if os.path.exists(os.path.join(".git", marker)):
            raise Blocked(f"unfinished-operation:{marker}",
                          f"A previous git operation is unfinished ({marker}). Resolve it manually; refusing to commit on top of it.")

    current = _git_out(["symbolic-ref", "--short", "-q", "HEAD"])
    if not current:
        raise Blocked("detached-head", "Detached HEAD. Refusing to commit.")

    expected = expected_branch(current)
    if current != expected:
        raise Blocked(f"wrong-branch:{current}",
                      f"Repo is on '{current}' but '{expected}' is expected. Refusing to commit/sync from the wrong branch "
                      f"(this is how deploy dirs end up with missing files). Fix with: git checkout {expected}")

    # Optional (AUTOSYNC_REQUIRE_GITIGNORE=1): for whitelist-style repos like /opt/stacks, where the
    # .gitignore itself is tracked. Without it, `git add .` would stage every database and secret.
    if os.environ.get("AUTOSYNC_REQUIRE_GITIGNORE") == "1" and not os.path.isfile(".gitignore"):
        raise Blocked("no-gitignore", "No .gitignore in the working tree; `git add .` would stage everything. Refusing.")
    return current

def guard_mass_deletions():
    """After `git add .`: refuse if the commit would delete suspiciously many tracked files."""
    if run_command(["git", "rev-parse", "--verify", "HEAD"], capture_output=True, suppress_errors=True).returncode != 0:
        return
    limit = int(os.environ.get("AUTOSYNC_MAX_DELETIONS", "3"))
    proc = run_command(["git", "diff", "--cached", "--name-only", "--diff-filter=D"], capture_output=True)
    deleted = sorted(l for l in (proc.stdout or "").splitlines() if l.strip())
    if len(deleted) > limit and os.environ.get("AUTOSYNC_ALLOW_DELETIONS") != "1":
        run_command(["git", "reset", "-q"])  # unstage only; nothing on disk is touched
        shown = ", ".join(deleted[:10]) + (f" ... and {len(deleted) - 10} more" if len(deleted) > 10 else "")
        digest = hashlib.sha1("\n".join(deleted).encode("utf-8")).hexdigest()[:10]
        raise Blocked(f"mass-deletion:{digest}",
                      f"Refusing to commit: {len(deleted)} tracked files are MISSING from disk (limit {limit}): {shown}. "
                      f"Investigate (git reflog, git status). If intentional, re-run once with AUTOSYNC_ALLOW_DELETIONS=1.")

def _push_ref(branch_name, max_retries=3):
    last = "Unknown Error"
    for attempt in range(1, max_retries + 1):
        print(f"🚀 Pushing '{branch_name}' to origin (Attempt {attempt}/{max_retries})...")
        r = run_command(["git", "push", "origin", f"refs/heads/{branch_name}:refs/heads/{branch_name}"], capture_output=True)
        if r.returncode == 0:
            for out in (r.stdout, r.stderr):
                if out and out.strip():
                    print(out.strip())
            return True, ""
        err_low = (r.stderr or "").lower()
        last = (r.stderr or "Unknown Error").strip()
        if "permission denied" in err_low or "authentication failed" in err_low:
            return False, "authentication error during push"
        if "gh001" in err_low or "larger than github's recommended maximum file size" in err_low:
            return False, "file too large"
        if "rejected" in err_low and ("non-fast-forward" in err_low or "fetch first" in err_low):
            return False, "push rejected (remote moved during sync); will retry next run"
        if attempt < max_retries:
            jitter_sleep = random.randint(25, 35)
            print(f"⚠️ Push failed on '{branch_name}'. Error: {last}\nWaiting {jitter_sleep}s before retry...")
            time.sleep(jitter_sleep)
    return False, f"push failed after max retries: {last}"

def _rebase_in_temp_worktree(branch_name, remote_ref):
    """Rebase a NON-checked-out branch onto its remote in a throwaway worktree."""
    wt = tempfile.mkdtemp(prefix="autosync_wt_")
    local_ref = f"refs/heads/{branch_name}"
    try:
        old_sha = _git_out(["rev-parse", local_ref])
        add = run_command(["git", "worktree", "add", "-q", "--detach", wt, local_ref], capture_output=True)
        if add.returncode != 0:
            return False, f"could not create temp worktree: {(add.stderr or '').strip()}"
        rb = subprocess.run(["git", "rebase", remote_ref], cwd=wt, text=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rb.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=wt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return False, "diverged from origin and rebase conflicts; manual merge required"
        new_sha = _git_out(["rev-parse", "HEAD"], cwd=wt)
        if subprocess.run(["git", "update-ref", local_ref, new_sha, old_sha]).returncode != 0:
            return False, "branch moved while rebasing; will retry next run"
        print(f"🔀 Rebased '{branch_name}' onto origin in a temporary worktree.")
        return True, ""
    finally:
        run_command(["git", "worktree", "remove", "--force", wt], suppress_errors=True)
        shutil.rmtree(wt, ignore_errors=True)
        run_command(["git", "worktree", "prune"], suppress_errors=True)

def sync_other_branch(branch_name, max_retries=3):
    """
    Syncs a branch that is NOT checked out, without touching the working tree.
    Returns (success: bool, reason: str).
    """
    print(f"\n=== Branch: {branch_name} (not checked out; working tree untouched) ===")
    if _checked_out_elsewhere(branch_name):
        print(f"⏭️  '{branch_name}' is checked out in another worktree right now. Skipping this run.")
        return True, ""

    local_ref, remote_ref = f"refs/heads/{branch_name}", f"refs/remotes/origin/{branch_name}"
    remote_exists, last_err = None, ""
    for attempt in range(1, max_retries + 1):
        print(f"⬇️  Fetching origin/{branch_name} (Attempt {attempt}/{max_retries})...")
        f = run_command(["git", "fetch", "-q", "origin", f"+{local_ref}:{remote_ref}"], capture_output=True)
        if f.returncode == 0:
            remote_exists = True
            break
        err_low = (f.stderr or "").lower()
        last_err = (f.stderr or "").strip()
        if "couldn't find remote ref" in err_low:
            print(f"ℹ️  No remote branch 'origin/{branch_name}' yet. Will create it on push.")
            remote_exists = False
            break
        if "permission denied" in err_low or "authentication failed" in err_low:
            return False, "authentication error during fetch"
        if attempt < max_retries:
            jitter_sleep = random.randint(25, 35)
            print(f"⚠️ Fetch failed. Retrying in {jitter_sleep}s...")
            time.sleep(jitter_sleep)
    if remote_exists is None:
        return False, f"fetch failed after max retries: {last_err}"

    if remote_exists:
        local_sha, remote_sha = _git_out(["rev-parse", local_ref]), _git_out(["rev-parse", remote_ref])
        if local_sha == remote_sha:
            print("✅ Already in sync.")
            return True, ""
        if _is_ancestor(local_sha, remote_sha):  # purely behind: just move the ref
            subprocess.run(["git", "update-ref", local_ref, remote_sha, local_sha])
            print(f"⏩ Fast-forwarded local '{branch_name}' to origin (no checkout).")
            return True, ""
        if not _is_ancestor(remote_sha, local_sha):  # diverged
            ok, reason = _rebase_in_temp_worktree(branch_name, remote_ref)
            if not ok:
                return False, reason
    return _push_ref(branch_name, max_retries)


def main():
    # ── UNIVERSAL AUTOMATION SAFEGUARDS (Cross-Platform) ──────────────────
    # BatchMode: Stops SSH from asking questions/prompts on all systems.
    # StrictHostKeyChecking=accept-new: Quietly registers Codeberg/GitHub keys on first run, but prevents hangs.
    # ConnectTimeout=15: Safe initial handshake
    # ServerAliveInterval=15 & ServerAliveCountMax=3: Abort if the server stops responding for 45 seconds mid-transfer
    # GIT_TERMINAL_PROMPT=0: Prevents Git from prompting for usernames/passwords.
    ssh_opts = "-q -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
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

    exit_code = 1
    try:
        problems = {}
        try:
            # 3. Safety gates BEFORE anything is staged
            current = preflight_checks()

            # 4. Stage all changes, then refuse mass deletions
            run_command(["git", "add", "."])
            guard_mass_deletions()

            # 5. Commit ONLY if there are changes
            head_check = run_command(["git", "rev-parse", "--verify", "HEAD"], capture_output=True, suppress_errors=True)
            fresh_repo = head_check.returncode != 0
            diff_check = run_command(["git", "diff-index", "--quiet", "HEAD", "--"]) if not fresh_repo else None

            if fresh_repo or (diff_check and diff_check.returncode != 0):
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                run_command(["git", "commit", "-m", f"{label}: {timestamp}"])
            else:
                print("Everything up-to-date.")

            # 6. Discover all local branches
            branches = get_local_branches()
            if not branches:
                print("❌ Error: No local branches found (not a git repository?).")
                sys.exit(1)
            print(f"\n📋 Found {len(branches)} local branch(es): {', '.join(branches)}")

            # 7. Checked-out branch in place; every other branch WITHOUT checkout
            results = {current: sync_checked_out_branch(current)}
            for branch in branches:
                if branch != current:
                    results[branch] = sync_other_branch(branch)

            # 8. Summary
            print("\n--- Sync Summary ---")
            for branch, (ok, reason) in results.items():
                if ok:
                    print(f"✅ {branch}: synced successfully")
                else:
                    print(f"❌ {branch}: FAILED — {reason}")
                    problems[f"branch:{branch}:{reason.split(':')[0].strip()}"] = f"{branch}: {reason}"
        except Blocked as blocked:
            print(f"❌ {blocked.message}")
            problems[blocked.signature] = blocked.message

        # 9. Alarm-once: exit 1 only for NEW problems
        exit_code = finalize_alarms(problems)
    finally:
        _unlock(lock_fd)
        lock_fd.close()
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
