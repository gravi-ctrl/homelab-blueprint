#!/usr/bin/env python3
# @DESCRIPTION: Rotates backups by keeping the N newest items per folder, or purging a folder's contents. Per-folder --keep/--purge flags, --dry-run preview, and safety guards against dangerous targets.
# @CAPABILITIES:
#   • Keeps only the newest N backups per folder, with per-folder --keep N overrides in a single call.
#   • --purge empties a folder's contents while leaving the folder itself in place.
#   • --dry-run previews every deletion, eliminating accidental data loss during testing.
#   • Refuses system directories, drive roots, Windows system folders, home, its own folder, symlink targets and overlapping folders.
#   • Validates every folder before deleting anything, so a bad argument never causes a partial run.
#   • Skips hidden entries (such as Syncthing's .stfolder) unless --hidden is given.
#   • Aborts a folder on the first deletion error, preventing cascading failures and preserving remaining data.

import argparse
import os
import shutil
import stat
import sys
from dataclasses import dataclass

DEFAULT_KEEP = 2  # items kept per folder when no --keep/--purge is given

# Never valid targets: these exact directories, or any directory that contains one of them.
SYSTEM_DIRS = ("bin", "boot", "dev", "etc", "home", "lib", "lib64", "media", "mnt",
               "opt", "proc", "root", "run", "sbin", "srv", "sys", "tmp", "usr", "var")
WINDOWS_SYSTEM_VARS = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
ALWAYS_SKIP = {".stfolder"}  # Syncthing's folder marker: deleting it makes Syncthing stop the folder


class UsageError(Exception):
    """Bad command line."""


class SafetyError(Exception):
    """A target failed a safety check; the whole run is aborted before deleting anything."""


class FolderMissing(Exception):
    """Target folder doesn't exist: reported as an error, but other folders still run."""


@dataclass
class Plan:
    folder: str
    limit: int
    dev: int
    ino: int
    total: int
    doomed: list  # [(path, dev, ino), ...]


# ---------------------------------------------------------------- safety checks

def protected_paths():
    found = {os.path.realpath(os.sep)}
    if os.name == "nt":
        # Standard Windows variables, always defined by the OS itself.
        for var in WINDOWS_SYSTEM_VARS:
            value = os.environ.get(var, "").strip()
            if value:
                found.add(os.path.realpath(value))
        found.add(os.path.dirname(os.path.realpath(os.path.expanduser("~"))))  # e.g. C:/Users
    else:
        found.update(os.path.realpath(os.path.join(os.sep, name)) for name in SYSTEM_DIRS)
    found.add(os.path.dirname(os.path.realpath(__file__)))
    found.add(os.path.realpath(os.path.expanduser("~")))
    return {os.path.normcase(p) for p in found}


def resolve_target(raw):
    """Validates a target and returns (canonical_path, lstat_result)."""
    if not raw.strip():
        raise SafetyError("empty folder path")
    if not os.path.isabs(raw):
        raise SafetyError(f"folder path must be absolute: '{raw}'")

    path = os.path.normpath(raw)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise FolderMissing(f"The folder '{path}' was not found.")
    except OSError as e:
        raise SafetyError(f"cannot inspect '{path}': {e}")

    if stat.S_ISLNK(st.st_mode):
        raise SafetyError(f"'{path}' is a symlink; pass the real path instead (e.g. from `readlink -f`)")
    if not stat.S_ISDIR(st.st_mode):
        raise SafetyError(f"'{path}' is not a directory")

    canonical = os.path.realpath(path)
    if os.path.dirname(canonical) == canonical:
        raise SafetyError(f"'{canonical}' is a drive/filesystem root")
    key = os.path.normcase(canonical)
    protected = protected_paths()
    if key in protected:
        raise SafetyError(f"'{canonical}' is a protected directory")
    prefix = key if key.endswith(os.sep) else key + os.sep
    for p in protected:
        if p.startswith(prefix):
            raise SafetyError(f"'{canonical}' contains the protected location '{p}'")

    return canonical, os.lstat(canonical)


def get_best_timestamp(st):
    if os.name == "nt":
        return st.st_ctime_ns  # Windows: true creation time
    return st.st_mtime_ns      # Linux: mtime avoids false ctime updates (chmod, chown, etc.)


def build_plan(raw, limit, include_hidden):
    folder, folder_st = resolve_target(raw)

    try:
        items = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name in ALWAYS_SKIP or (entry.name.startswith(".") and not include_hidden):
                    continue
                # os.lstat, not entry.stat(): on Windows DirEntry.stat() leaves st_dev/st_ino at 0.
                st = os.lstat(entry.path)
                items.append((get_best_timestamp(st), entry.name, entry.path, st))
    except OSError as e:
        raise SafetyError(f"cannot list '{folder}': {e}")

    items.sort(key=lambda i: (i[0], i[1]), reverse=True)  # newest first, name breaks ties

    doomed = []
    for _, _, path, st in items[limit:]:
        # A different device means a mount point sits inside the folder: never delete through it.
        if not stat.S_ISLNK(st.st_mode) and st.st_dev != folder_st.st_dev:
            raise SafetyError(f"'{path}' is on another filesystem (mount point?); refusing to delete it")
        doomed.append((path, st.st_dev, st.st_ino))

    return Plan(folder, limit, folder_st.st_dev, folder_st.st_ino, len(items), doomed)


def check_overlaps(plans):
    paths = [os.path.normcase(p.folder) for p in plans]
    for i, a in enumerate(paths):
        for b in paths[i + 1:]:
            if a == b:
                raise SafetyError(f"the same folder was given more than once: '{a}'")
            if b.startswith(a.rstrip(os.sep) + os.sep) or a.startswith(b.rstrip(os.sep) + os.sep):
                raise SafetyError(f"overlapping folders in one run: '{a}' and '{b}'")


# ---------------------------------------------------------------- cleanup

def clean_folder(plan, dry_run):
    """Deletes the planned items. Returns True if successful, False on the first error."""
    if not plan.doomed:
        print(f"✅ Nothing to delete: {plan.total} item(s), limit is {plan.limit}.")
        return True

    for path, dev, ino in plan.doomed:
        if dry_run:
            print(f"🕵️ [DRY RUN] Would delete: {path}")
            continue
        try:
            # Make sure nothing was swapped out between validation and deletion.
            cur = os.lstat(plan.folder)
            if stat.S_ISLNK(cur.st_mode) or (cur.st_dev, cur.st_ino) != (plan.dev, plan.ino):
                raise OSError("target folder changed after validation")
            cur = os.lstat(path)
            if (cur.st_dev, cur.st_ino) != (dev, ino):
                raise OSError("item changed after validation")

            if stat.S_ISDIR(cur.st_mode):
                shutil.rmtree(path)
                print(f"🗑️ Deleted folder: {os.path.basename(path)}")
            else:
                os.unlink(path)  # files and symlinks (the link itself, never its target)
                print(f"🗑️ Deleted file: {os.path.basename(path)}")
        except OSError as e:
            print(f"❌ Error deleting {path}: {e}")
            return False
    return True


# ---------------------------------------------------------------- command line

def build_help_parser():
    # Only used to render -h; the real parsing is order-sensitive and done in parse_args().
    return argparse.ArgumentParser(
        prog="cleanup_script.py",
        usage="%(prog)s [-h] [--dry-run] [--hidden] FOLDER [--keep N] FOLDER [--purge] FOLDER ...",
        description="Rotate backups: keep the newest N items in each folder and delete the rest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "FOLDER           a backup folder (absolute path). With no flag before it, keeps the\n"
            f"                 newest {DEFAULT_KEEP} items.\n"
            "--keep N FOLDER  keep the newest N items in FOLDER (N >= 1).\n"
            "--purge FOLDER   delete EVERYTHING inside FOLDER (the folder itself stays).\n"
            "\n"
            "--keep and --purge only affect the single FOLDER that follows them.\n"
            "--dry-run        preview what would be deleted, without deleting anything.\n"
            "--hidden         also count/delete hidden entries (dotfiles). Skipped by default;\n"
            "                 Syncthing's .stfolder is never touched.\n"
            "\n"
            "Safety: everything is validated before anything is deleted. Refused: relative\n"
            "paths, symlink targets, drive roots, system directories (/, /home, /opt, /tmp, ...\n"
            "or C:\\Windows, Program Files, C:\\Users on Windows), your home\n"
            "directory, this script's folder, folders that contain any of those, and\n"
            "overlapping folders. Subfolders of the protected ones are fine.\n"
            "A folder that doesn't exist is reported as an error, but the others still run.\n"
            "\n"
            "Exit codes: 0 ok, 1 error or refusal, 2 bad usage.\n"
            "\n"
            "example:\n"
            "  cleanup_script.py \\\n"
            '    "$DATA_DIR/Backup/contacts" \\\n'
            '    --keep 1 "$DATA_DIR/Backup/antennapod" \\\n'
            '    --purge "$DATA_DIR/downloads/watch"'
        ),
    )


def parse_args(args):
    """Returns (targets, dry_run, include_hidden). --keep/--purge apply to the folder right after them."""
    targets = []
    dry_run = False
    include_hidden = False
    pending_keep = None
    pending_purge = False
    i = 0
    while i < len(args):
        tok = args[i]
        i += 1

        if tok == "--dry-run":
            dry_run = True
        elif tok == "--hidden":
            include_hidden = True
        elif tok == "--purge":
            if pending_purge or pending_keep is not None:
                raise UsageError("--purge/--keep given twice for the same folder")
            pending_purge = True
        elif tok == "--keep" or tok.startswith("--keep="):
            if pending_purge or pending_keep is not None:
                raise UsageError("--purge/--keep given twice for the same folder")
            if tok == "--keep":
                if i >= len(args):
                    raise UsageError("--keep needs a number (e.g. --keep 1)")
                value = args[i]
                i += 1
            else:
                value = tok.split("=", 1)[1]
            try:
                pending_keep = int(value)
            except ValueError:
                raise UsageError(f"--keep expects a whole number, got '{value}'")
            if pending_keep < 1:
                raise UsageError("--keep must be at least 1 (use --purge to delete everything)")
        elif tok.startswith("--"):
            raise UsageError(f"unknown option '{tok}'")
        else:
            limit = 0 if pending_purge else (pending_keep if pending_keep is not None else DEFAULT_KEEP)
            targets.append((tok, limit))
            pending_keep = None
            pending_purge = False

    if pending_purge:
        raise UsageError("--purge must be followed by a folder")
    if pending_keep is not None:
        raise UsageError("--keep N must be followed by a folder")
    if not targets:
        raise UsageError("No backup folders provided.")
    return targets, dry_run, include_hidden


def main():
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        build_help_parser().print_help()
        return 0

    try:
        targets, dry_run, include_hidden = parse_args(args)
    except UsageError as e:
        print(f"❌ Error: {e}")
        print("   Run with -h for usage.")
        return 2

    # Validate and plan EVERY folder before deleting anything.
    entries = []   # (raw, limit, plan or None, missing-message or None)
    refusals = []
    for raw, limit in targets:
        try:
            entries.append((raw, limit, build_plan(raw, limit, include_hidden), None))
        except FolderMissing as e:
            entries.append((raw, limit, None, str(e)))
        except SafetyError as e:
            refusals.append(f"{raw!r}: {e}")

    if not refusals:
        try:
            check_overlaps([plan for _, _, plan, _ in entries if plan])
        except SafetyError as e:
            refusals.append(str(e))

    if refusals:
        print("❌ Refusing to run. Nothing was deleted:")
        for r in refusals:
            print(f"  • {r}")
        return 1

    if dry_run:
        print("🧪 --- DRY RUN MODE ACTIVE (No files will be deleted) ---")
    else:
        print("🧹 --- Starting Backup Cleanup ---")

    any_error = False
    for raw, limit, plan, missing in entries:
        if limit == 0:
            print(f"📂 Mode: PURGE ALL for '{raw}'")
        else:
            print(f"📂 Mode: ROTATE (Keep {limit}) for '{raw}'")

        if missing:
            print(f"❌ Error: {missing}")
            any_error = True
        elif not clean_folder(plan, dry_run):
            any_error = True

    print("✨ --- Cleanup Process Finished ---")
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())