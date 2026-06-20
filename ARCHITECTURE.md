# Architecture & Design Notes

This file documents *why* things are wired the way they are: the fixed anchor points, the
non-obvious gotchas, and the places where multiple scripts quietly depend on each other.
It complements the [README](./README.md): the README is the runbook ("what to run, in what
order"); this is the reference for "why does this work, and what happens if I change it."

---

## 1. The Fixed Anchors

Three paths are treated as permanent, never-renamed contracts. Everything else in the repo
is free to vary.

| Path | What it is | Set in |
|---|---|---|
| `/opt/ctrl` | Symlink to the real scripts repo (`$HOME/scripts`) | `setup.sh` |
| `/opt/venv` | The Python virtualenv all scripts run under | `setup.sh` |
| `/opt/stacks` | Docker Compose stacks (separate repo) | `setup.sh` / `bootstrap.sh` |

**The one variable that's allowed to change** is `MY_SCRIPTS="$HOME/scripts"` in `setup.sh`, this is where the *real* files live; `/opt/ctrl` is just the alias every other script points
at. If the real location ever moves (different mount, different folder name), only this one
line needs editing. `/opt/ctrl` itself should never be renamed without a full sweep of every
script that hardcodes it, `setup.sh` included, since it both *creates* the symlink and
*consumes* the alias internally (cron-guard symlink, `.env` checks, systemd `ExecStart`, etc).

A renaming sweep should specifically **not** touch `$HOME` or `$HOME/scripts` patterns,
those are generic and appear all over the place for unrelated reasons (dotfiles, `.ssh`,
mkcert). Only the literal string `/opt/ctrl` is safe to blind `grep`/`sed`.

---

## 2. Ownership & the Symlink Dereference Trap

`/opt/ctrl` is created via `sudo ln -sf "$MY_SCRIPTS" /opt/ctrl`, so **the symlink itself is
root-owned**, even though the directory it points to belongs to you.

This matters because GNU `stat` defaults to **not** following symlinks (same default as
`ls -l`). Stat the symlink bare, and you get root's metadata, not the target's:

```bash
stat -c '%U' /opt/ctrl       # → root   (the link's own owner, wrong)
stat -c '%U' /opt/ctrl/.     # → you    (trailing dot forces dereference, correct)
stat -L -c '%U' /opt/ctrl    # → you    (explicit flag, correct, and clearer to read)
```

**The rule:** this only matters when the symlink is the *final* component of the path.
Anything reached *through* `/opt/ctrl` as an intermediate component, `/opt/ctrl/some_file`,
`${BASH_SOURCE[0]}` when invoked as `/opt/ctrl/script.sh`, is resolved transparently by the
kernel during path lookup. No flag needed. This is why `local-opt-backup.sh`'s
`stat -c '%U' "${BASH_SOURCE[0]}"` and `vergil.py`'s `Path(__file__).resolve().owner()` are
both correct as-is: in both cases the *file itself* isn't a symlink, only a directory
component along the way is.

The one place the bare/dereferenced distinction is actually load-bearing: `container-watcher.sh`'s
`task_nextcloud()` uses `stat -L -c '%U' /opt/ctrl` (correctly dereferenced) to find the real
owner for a `sudo -u` call. Without `-L`, that would silently run as root.

---

## 3. Disaster-Recovery Ownership: UID/GID, Not Usernames

The backup → restore path never trusts usernames, only numeric IDs, and it tracks **both**
UID and GID independently, because they don't always move together.

**At backup time** (`local-opt-backup.sh`):
```bash
SCRIPT_OWNER=$(stat -c '%U' "${BASH_SOURCE[0]}")
echo "$(id -u "$SCRIPT_OWNER"):$(id -g "$SCRIPT_OWNER")" > /tmp/backup-uid.txt
```
This numeric pair travels inside the encrypted archive.

**At restore time** (`bootstrap.sh`):
```bash
IFS=: read -r B_UID B_GID < /tmp/backup-uid.txt
sudo find "$DIR_STACKS" "$DIR_SCRIPTS" "$DIR_CTRL" "$HOME/.ssh" \
    \( -uid "$B_UID" -o -gid "$B_GID" \) \
    ! \( -uid "$(id -u)" -a -gid "$(id -g)" \) \
    -exec chown "$(id -u):$(id -g)" {} +
```

**Why both fields, not just UID:** `useradd` does *not* guarantee UID and GID move together.
If the new server's account lands on the same UID as the old one (likely, since most distros
start regular accounts at UID 1000 regardless of which physical box you're on) but a different
GID (because some other group already occupied the expected slot on this particular image),
a UID-only check would silently miss the GID drift. The combined check, flag anything that
still carries *either* old number, fix unless it's *already* correct on both, closes that
gap. Scoped to a handful of private, non-shared directories, so it can never accidentally
touch files that legitimately belong to something else (container UIDs like `www-data:33`,
for instance).

---

## 4. `cron-guard --mode mute`: cosmetic origin, functional justification

`mute` mode didn't start out solving a real problem, it started as a labeling trick.

`homelab_dash.py` builds its UI from crontab comments and command text. For any cron line
that *doesn't* go through `cron-guard`, the dashboard falls back to truncating whatever
comment sits above the line. Wrapping a job through `cron-guard --mode <mode> "Job Name" ...`
— *any* mode, including `mute`, gives the dashboard a clean, structured name to parse out
via regex, instead of guessing from prose:

```python
cg_match = re.search(r'cron-guard(?:\.py)?\s+--mode\s+(?:fail|success|all|mute)\s+["\']([^"\']+)["\']', cmd)
```

So `mute` mode initially existed just so quiet jobs (no Telegram noise wanted) could still
get a tidy dashboard entry without a long explanatory comment. That alone would've made it a
thin wrapper around "run a command, log nothing."

**It earned a second, functional reason to exist** when the heartbeat file was added:
`mute` mode writes `status/<job>_<hash>.json` (last run time, exit code, duration, timed-out
flag) on every run, purely local, never sent anywhere, never read by the dashboard. It's the
only way to manually confirm a permanently-silent job actually ran, since by definition
nothing else will ever tell you.

---

## 5. The Dashboard Pipeline: isolated worktrees + a timestamp-blind diff

Daily at 5am, `backup-scripts-git.sh` triggers `generate-dashboards.sh`, which has to solve a
problem: how do you commit fresh generated HTML to a `pages` branch without disturbing
whatever's currently checked out in the *main* working directory (which might have real,
uncommitted work in progress)?

**Answer: a temporary git worktree**, not a branch switch in place:
```bash
git -C "$CURRENT_REPO" worktree add "$CURRENT_WORKTREE" pages
git -C "$CURRENT_WORKTREE" pull --rebase origin pages
cp "$temp_html" "$CURRENT_WORKTREE/index.html"
```
The worktree is a separate checkout sharing the same object database, commits made there are
real commits on the repo's `pages` branch, but the primary working directory's checked-out
branch and any uncommitted state are never touched. A `trap ... EXIT INT TERM` guarantees the
worktree gets torn down (`rm -rf` + `worktree prune`) no matter how the script exits.

**The noise filter**, every generated dashboard embeds a `Generated <timestamp>` line that
changes on literally every run, even when nothing else did. Committing that daily would bury
real changes in timestamp-only noise:
```bash
git diff --cached --quiet -I "[Gg]enerated "
```
`git diff -I<regex>` treats any diff hunk where *every* changed line matches the regex as
unchanged. Since the timestamp line is the only thing that ever changes on a no-op day, this
correctly detects "nothing of substance changed" and skips the commit.

**Why the explicit push at the end still works:** `git-auto-sync.py` (the generic multi-repo
sync engine used elsewhere) treats `pages` as just another local branch once it exists,
`get_local_branches()` enumerates *every* `refs/heads/*` and syncs each one. So once
`generate-dashboards.sh` has created `pages` for the first time, the regular daily sync step
picks it up automatically going forward, with no special-casing needed.

**The dependency to keep in mind:** this only works because the worktree is always cleaned up
by the time anything else tries to check out `pages` in the *main* working tree. Git refuses
to have the same branch checked out in two places at once. The trap-based cleanup is what
makes that guarantee hold, if a future change ever skipped or broke the trap, the next
sync cycle's attempt to check out `pages` directly would fail with "already checked out
elsewhere."

---

## 6. The Day-of-Month / Day-of-Week OR Trap

```
0 2 8-14,22-28 * * [ "$(date +\%u)" = 5 ] && cron-guard --mode fail "ctrl_s_master" "$CTRL_DIR/run.sh"
```

This line is intended to mean "the 2nd and 4th Friday of the month." Writing that the
"obvious" way, `0 2 8-14,22-28 * 5 ...`, restricting day-of-month *and* day-of-week,
would not work: cron treats those two fields as **OR**, not AND, whenever both are
restricted (not `*`). That version would actually fire on day-of-month 8–14 or 22–28, **or**
any Friday at all, every single Friday of the month, plus eight extra days.

The fix: leave day-of-week as `*` at the cron level (so cron only filters by date), and do
the real day-of-week check **inside the command** via `date +%u`, combined with `&&`. That
gets true AND semantics by moving the second condition out of cron's field-matching entirely.

`homelab_dash.py` has a regex specifically built to recognize this `date +\%[uwaA] ... &&`
pattern and render it as a clean human label ("2nd and 4th Friday") instead of dumping raw
cron syntax. It's intentionally general, not special-cased to this one line, built to catch
*any* date-conditional cron job written this way, so future irregular schedules get the same
readable treatment automatically.

---

## 7. GitOps Conflict Handling: Alert Once, Then Go Quiet

`gitops-deploy.sh` runs every 15 minutes and needs its own alert-fatigue strategy, separate
from `cron-guard --mode mute`. On a merge conflict during `git pull --rebase --autostash`:

```bash
if ! git pull -q origin main --rebase --autostash; then
    if [ ! -f "$PAUSE_FILE" ]; then
        send_telegram "🚨 GitOps Paused: Merge Conflict Detected..."
        touch "$PAUSE_FILE"
    fi
    exit 0   # not exit 1, deliberately suppresses cron-guard's --mode fail alert
fi
```

If this just let `--mode fail` catch the failure normally, the same conflict would re-trigger
a Telegram alert every 15 minutes until manually resolved, exactly the kind of noise `mute`
mode exists to prevent elsewhere, but this needed a different mechanism since the job isn't
silent by default, it's *fail-alerting* by default and needs to suppress itself only after
the first hit.

The pattern: send exactly one detailed alert, drop a marker file, then deliberately exit
clean on every subsequent run so cron-guard sees a "success" and stays quiet, "tell me once,
with everything I need, then go silent until I fix it." The alert itself doubles as the
recovery instructions: once you're back at the keyboard, resolving means `git restore .` (to
discard the conflicting local change) or staging and committing it properly, then a normal
push, at which point the next run's clean pull clears `$PAUSE_FILE` automatically and
alerting resumes as normal.

---

## 8. Known Accepted Risks

**`gitops-deploy.sh` (every 15 min) vs. the daily backup sync, on the same `/opt/stacks`
working directory.** Both scripts run `git checkout`/`pull`/`push` directly against
`$STACKS_DIR`, with no lock between them, unlike `local-opt-backup.sh`, which uses `flock`
for exactly this category of problem. They used to be scheduled at the literal same minute
(`gitops-deploy.sh`'s `*/15` includes `:00`; the backup ran at `0 5`). **Resolved by staggering**
the backup to `5 5 * * *` instead of building a lock, a probabilistic fix, not a guaranteed
one, but proportionate to the actual stakes (a once-daily git sync, not anything safety-critical).
If this ever needs a harder guarantee, `local-opt-backup.sh`'s `flock` pattern is the template
to copy.

**`cert-manager.sh`'s `setup-cron` command bypasses `cron-guard`.** The actual line in
`user_crontab.txt` is hand-wrapped through `cron-guard --mode fail`; `cmd_setup_cron()` would
install a different, unwrapped line if run. This is intentional, not a bug: `cert-manager.sh`
is the one script in the repo plausible to grab and run standalone, independent of the rest of
the cron-guard ecosystem, so it keeps its own self-contained installer as a fallback for that
case, even though the live deployment doesn't use it.

---

## 9. Quick Reference: Gotchas

- Stat-ing `/opt/ctrl` for ownership? Use `stat -L` or a trailing `/.`, the bare form returns
  root.
- Renaming `/opt/ctrl`? Sweep the literal string everywhere, `setup.sh` included. Never touch
  `$HOME`/`$HOME/scripts` patterns in the same sweep.
- Restoring ownership after a disaster recovery? Check UID *and* GID, they can drift
  independently even when the UID happens to match.
- Adding a new always-silent cron job? Use `cron-guard --mode mute`, you get a clean
  dashboard label *and* a heartbeat file for free.
- Touching `generate-dashboards.sh`'s cleanup trap? Don't remove it, the next sync cycle's
  ability to check out `pages` depends on the worktree always being gone by then.
- Writing a cron line with *both* day-of-month and day-of-week restricted? Cron ORs them, not
  ANDs. Leave day-of-week as `*` and check it inside the command instead.
- Building a new "noisy by default" job that can get stuck failing repeatedly (merge
  conflicts, lock contention, etc)? Look at `gitops-deploy.sh`'s pause-file pattern before
  reaching for `--mode mute`, mute silences everything, the pause-file approach alerts once
  then goes quiet only for that specific stuck state.
