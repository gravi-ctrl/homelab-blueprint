# Architecture & Design Notes

This file is for *why* things are wired the way they are — the anchor points I don't move,
the gotchas that aren't obvious until they bite, and the spots where two scripts quietly
depend on each other in a way that isn't visible from either file alone. The
[README](./README.md) is the runbook: what to run, in what order. This is the "why does this
actually work, and what breaks if I touch it" doc.

---

## 1. The Fixed Anchors

Three paths I treat as permanent. Nothing else in the repo is sacred, but these three don't
get renamed:

| Path | What it is | Set in |
|---|---|---|
| `/opt/rabbit-hole` | Symlink to the real scripts repo (`$HOME/scripts`) | `setup.sh` |
| `/opt/venv` | The Python virtualenv all scripts run under | `setup.sh` |
| `/opt/stacks` | Docker Compose stacks (separate repo) | `setup.sh` / `bootstrap.sh` |

The one thing that's actually allowed to move is `MY_SCRIPTS="$HOME/scripts"` in `setup.sh` —
that's where the real files live, and `/opt/rabbit-hole` is just the alias every other script
points at. If the real location ever changes (different mount, different folder name), that's
the only line to touch. `/opt/rabbit-hole` itself is a different story — renaming it means
sweeping every script that hardcodes it, `setup.sh` included, since it both creates the
symlink and consumes the alias internally (cron-guard symlink, `.env` checks, systemd
`ExecStart`, all of it).

One thing to watch if I ever do that sweep: don't touch plain `$HOME` or `$HOME/scripts`
patterns. Those show up all over the place for unrelated reasons — dotfiles, `.ssh`, mkcert —
and a blind sed across those would break things that have nothing to do with this. Only the
literal string `/opt/rabbit-hole` is safe to grep/sed for.

---

## 2. DNS Resolution Order: Why the Server's Own IP Comes First

```json
"dns": ["${SERVER_IP}", "8.8.8.8", "1.1.1.1"],
```

That order in `daemon.json` isn't arbitrary, and it's the one line in this whole setup that
exists because I got burned by not having it.

The domain everything runs under is a private one, issued through mkcert — it's not
registered anywhere public, and it never will be. Google and Cloudflare have no idea it
exists and never will. My own Unbound resolver (set up in setup.sh, step 7/10) is the only thing on the network
that can actually answer for it.

That matters because containers don't just serve on that domain, they *talk to each other*
across it. Nextcloud and n8n both authenticate through Pocket ID over OIDC, and OIDC isn't
just "does the login page load" — the container has to resolve the issuer's hostname itself
to hit the discovery endpoint and validate redirect URIs. If the first DNS server in that
list can't answer for the private zone, that resolution either fails outright or falls
through to something that isn't actually serving Pocket ID, and the whole auth flow dies with
errors that don't obviously say "this is a DNS problem."

Which is exactly what happened. I rebuilt the daemon.json by hand at some point, put Google
and Cloudflare first because that's the instinctive "safe" choice, and OIDC broke across
every container that used it. Took a while to connect "auth is broken" back to "oh, the
resolver order changed."

This is a known pattern, it's got a name — split-horizon DNS — and it's the standard approach
for anyone running an internal-only domain. But it's easy to "fix" by accident: reflexively
reaching for a public resolver first while debugging something unrelated will quietly break
every OIDC-dependent container, and the error you get won't point at DNS at all.

**The rule going forward:** `SERVER_IP` stays first in that list, always. If Docker DNS ever
needs debugging, check the order before touching anything else.

---

## 3. Ownership & the Symlink Dereference Trap

`/opt/rabbit-hole` gets created with `sudo ln -sf "$MY_SCRIPTS" /opt/rabbit-hole` — which means
the symlink itself is root-owned, even though the directory it points to is mine.

That trips people up because GNU `stat` doesn't follow symlinks by default (same as `ls -l`).
Stat the bare symlink and you get root's metadata, not the target's:

```bash
stat -c '%U' /opt/rabbit-hole       # → root   (the link's own owner — wrong)
stat -c '%U' /opt/rabbit-hole/.     # → you    (trailing dot forces dereference — correct)
stat -L -c '%U' /opt/rabbit-hole    # → you    (explicit flag — correct, and clearer to read)
```

This only bites when the symlink is the *last* component of the path. Anything reached
*through* it — `/opt/rabbit-hole/some_file`, or `${BASH_SOURCE[0]}` when a script is invoked as
`/opt/rabbit-hole/script.sh` — gets resolved transparently by the kernel, no flag needed.
That's why `local-opt-backup.sh`'s `stat -c '%U' "${BASH_SOURCE[0]}"` and `vergil.py`'s
`Path(__file__).resolve().owner()` both work fine as written: in both cases the file itself
isn't a symlink, only a directory earlier in the path is.

The one place this is actually load-bearing is `container-watcher.sh`'s `task_nextcloud()`,
which uses `stat -L -c '%U' /opt/rabbit-hole` — correctly dereferenced — to find the real owner
before a `sudo -u` call. Drop the `-L` there and it'd silently run as root.

---

## 4. Disaster-Recovery Ownership: UID/GID, Not Usernames

The backup/restore path doesn't trust usernames at all — only numeric IDs, and it tracks UID
and GID separately, because they don't always travel together.

At backup time (`local-opt-backup.sh`):
```bash
SCRIPT_OWNER=$(stat -c '%U' "${BASH_SOURCE[0]}")
echo "$(id -u "$SCRIPT_OWNER"):$(id -g "$SCRIPT_OWNER")" > /tmp/backup-uid.txt
```
That numeric pair rides along inside the encrypted archive.

At restore time (`bootstrap.sh`):
```bash
IFS=: read -r B_UID B_GID < /tmp/backup-uid.txt
sudo find "$DIR_STACKS" "$DIR_SCRIPTS" "$DIR_CTRL" "$HOME/.ssh" \
    \( -uid "$B_UID" -o -gid "$B_GID" \) \
    ! \( -uid "$(id -u)" -a -gid "$(id -g)" \) \
    -exec chown "$(id -u):$(id -g)" {} +
```

Why bother with both fields instead of just UID: `useradd` doesn't guarantee they land
together on a fresh box. Regular accounts tend to start at UID 1000 on most distros, so the
UID is likely to match across a rebuild — but the GID isn't, if some other group already
claimed that slot on the new image. A UID-only check would miss that drift entirely. Checking
both — flag anything still carrying *either* old number, fix it unless it's already correct
on both — closes that gap. It's scoped tightly to a handful of private directories, so it
can't accidentally reach into stuff that legitimately belongs to something else, like
container UIDs (`www-data:33` and friends).

---

## 5. `cron-guard --mode mute`: started cosmetic, became load-bearing

`mute` mode didn't set out to solve a real problem. It started as a labeling trick.

`homelab_dash.py` builds its UI off crontab comments and command text. Any cron line that
doesn't go through `cron-guard` gets whatever comment sits above it, truncated, guessed at.
Wrapping a job as `cron-guard --mode <mode> "Job Name" ...` — any mode, `mute` included — gives
the dashboard something structured to parse instead:

```python
cg_match = re.search(r'cron-guard(?:\.py)?\s+--mode\s+(?:fail|success|all|mute)\s+["\']([^"\']+)["\']', cmd)
```

So originally `mute` just existed so quiet jobs (no Telegram noise wanted) could still show up
cleanly on the dashboard without a long comment explaining themselves. On its own that would've
made it a fairly thin wrapper — run a command, log nothing.

It picked up a real second reason to exist once the heartbeat file showed up: `mute` mode
writes `status/<job>_<hash>.json` (last run time, exit code, duration, timed-out flag) on
every run, purely local, never sent anywhere or read by the dashboard. It's the only way to
manually confirm a permanently-silent job actually ran — since by design, nothing else is
ever going to tell you.

---

## 6. The Dashboard Pipeline: isolated worktrees + a timestamp-blind diff

Daily at 5am, `backup-scripts-git.sh` kicks off `generate-dashboards.sh`, which has to solve a
real problem: how do you commit freshly generated HTML to a `pages` branch without disturbing
whatever's checked out in the main working directory — which might have real, uncommitted
work sitting in it?

The answer is a temporary git worktree, not a branch switch in place:
```bash
git -C "$CURRENT_REPO" worktree add "$CURRENT_WORKTREE" pages
git -C "$CURRENT_WORKTREE" pull --rebase origin pages
cp "$temp_html" "$CURRENT_WORKTREE/index.html"
```
A worktree is a separate checkout sharing the same object database, so commits made there are
real commits on `pages`, but the main working directory's checked-out branch and any
uncommitted state never get touched. A `trap ... EXIT INT TERM` guarantees the worktree gets
torn down (`rm -rf` + `worktree prune`) no matter how the script exits.

There's also a noise problem: every generated dashboard embeds a `Generated <timestamp>` line
that changes on literally every run, whether or not anything else did. Committing that daily
would bury real changes under timestamp-only noise, so:
```bash
git diff --cached --quiet -I "[Gg]enerated "
```
`git diff -I<regex>` treats a diff hunk as unchanged if every changed line matches the regex.
Since the timestamp is the only thing that ever moves on a no-op day, this correctly detects
"nothing of substance changed" and skips the commit.

Why the explicit push at the end still works without extra wiring: `git-auto-sync.py` (the
generic multi-repo sync engine used elsewhere) treats `pages` as just another local branch
once it exists — `get_local_branches()` walks every `refs/heads/*` and syncs each one. So once
`generate-dashboards.sh` has created `pages` for the first time, the regular daily sync picks
it up automatically from then on, no special-casing needed.

One dependency worth remembering: this only works because the worktree is always gone by the
time anything else tries to check out `pages` in the main working tree. Git won't let the
same branch be checked out in two places at once. The trap-based cleanup is what makes that
hold — if a future change ever skipped or broke the trap, the next sync cycle's attempt to
check out `pages` directly would fail with "already checked out elsewhere."

---

## 7. The Day-of-Month / Day-of-Week OR Trap

```
0 2 8-14,22-28 * * [ "$(date +\%u)" = 5 ] && cron-guard --mode fail "ctrl_s_master" "$CTRL_DIR/run.sh"
```

This is meant to run on the 2nd and 4th Friday of the month. Writing it the "obvious" way —
`0 2 8-14,22-28 * 5 ...`, restricting both day-of-month and day-of-week — doesn't work. Cron
ORs those two fields whenever both are restricted, it doesn't AND them. That version would
actually fire on day-of-month 8–14 or 22–28, *or* any Friday at all — every Friday of the
month, plus eight extra days.

The fix is to leave day-of-week as `*` at the cron level, so cron only filters by date, and do
the actual day-of-week check inside the command with `date +%u` and `&&`. That gets real AND
semantics by pulling the second condition out of cron's field-matching entirely.

`homelab_dash.py` has a regex built specifically to recognize this `date +\%[uwaA] ... &&`
pattern and render it as something readable ("2nd and 4th Friday") instead of dumping raw
cron syntax at me. It's written generally, not hardcoded to this one line, so any future
irregular schedule written the same way gets the same treatment automatically.

---

## 8. GitOps Conflict Handling: Alert Once, Then Go Quiet

`gitops-deploy.sh` runs every 15 minutes and needed its own alert-fatigue handling, separate
from `cron-guard --mode mute`. On a merge conflict during `git pull --rebase --autostash`:

```bash
if ! git pull -q origin main --rebase --autostash; then
    if [ ! -f "$PAUSE_FILE" ]; then
        send_telegram "🚨 GitOps Paused: Merge Conflict Detected..."
        touch "$PAUSE_FILE"
    fi
    exit 0   # not exit 1 — deliberately suppresses cron-guard's --mode fail alert
fi
```

If this just let `--mode fail` catch the failure normally, the same conflict would re-trigger
a Telegram alert every 15 minutes until I manually fixed it — exactly the noise `mute` mode
exists to prevent elsewhere, except this job isn't silent by default, it's fail-alerting by
default, and needs to suppress *itself* only after the first hit.

So the pattern is: send one detailed alert, drop a marker file, then deliberately exit clean
on every run after that, so cron-guard sees "success" and stays quiet. Tell me once, with
everything I need, then go silent until I fix it. The alert doubles as the recovery
instructions — once I'm back at a keyboard, resolving it means either `git restore .` to
discard the conflicting local change, or staging and committing it properly, then a normal
push. The next clean pull clears `$PAUSE_FILE` on its own, and alerting resumes as normal.

---

## 9. Repo Locking Convention

Any script that mutates a git repo's working tree (`checkout`, `pull`, `push`) has to hold a
non-blocking `flock` on that repo's own `.git/sync.lock` before doing anything, and release it
on exit. This replaced an earlier approach of just staggering the schedules between
`gitops-deploy.sh` (every 15 min) and the daily backup sync, which both touched `$STACKS_DIR`
directly with zero coordination and, at one point, were scheduled at the literal same minute.
Staggering reduced the odds of collision. Locking removes the race entirely.

**Bash** (`gitops-deploy.sh`):
```bash
exec 200>"${STACKS_DIR}/.git/sync.lock"
flock -n 200 || { echo "⏭️  Stacks repo busy, skipping this cycle."; exit 0; }
```
The lock releases automatically when the fd closes at script exit — nothing explicit needed.

**Python** (`git-auto-sync.py`): same idea, `fcntl.flock(..., LOCK_EX | LOCK_NB)` on Linux,
`msvcrt.locking()` on Windows, branched once on `sys.platform` — same shape as the
OS-conditional process-tree kill in `cron-guard.py`. Acquired right after `chdir` into the
target repo, held for the whole run via `try`/`finally`, released on every exit path including
`sys.exit()` (which raises `SystemExit`, so `finally` still runs).

Why `-n` / `LOCK_NB` instead of waiting: `gitops-deploy.sh` runs again in 15 minutes
regardless, so skipping one cycle and catching the deploy next time costs nothing. A
multi-second wait would also eat into whatever timeout `cron-guard` has set for that job, for
no upside.

Why the lock file specifically lives at `<repo>/.git/sync.lock`: it makes the convention apply
to any future script without needing a registry to remember it. The rule is just — touching
this repo's git state? Lock its own `.git/sync.lock` first.

---

## 10. Known Trade-offs

`user_crontab.txt` is hand-wrapped through `cron-guard --mode fail`, but `cmd_setup_cron()`
would install a different, unwrapped line if I ran it. That's intentional, not a bug —
`cert-manager.sh` is the one script in the repo plausible to grab and run standalone,
independent of the rest of the cron-guard ecosystem, so it keeps its own self-contained
installer as a fallback for that case, even though the live deployment doesn't actually use
it.

---

## 11. Quick Reference: Gotchas

- Docker DNS acting weird, or OIDC breaking across containers? Check `daemon.json` — `SERVER_IP`
  needs to be first in the list, not Google or Cloudflare. See §2.
- Stat-ing `/opt/rabbit-hole` for ownership? Use `stat -L` or a trailing `/.` — the bare form
  returns root.
- Renaming `/opt/rabbit-hole`? Sweep the literal string everywhere, `setup.sh` included. Never
  touch `$HOME`/`$HOME/scripts` patterns in the same sweep.
- Restoring ownership after a disaster recovery? Check UID *and* GID — they can drift
  independently even when the UID happens to match.
- Adding a new always-silent cron job? Use `cron-guard --mode mute` — clean dashboard label
  and a heartbeat file for free.
- Touching `generate-dashboards.sh`'s cleanup trap? Don't remove it — the next sync cycle's
  ability to check out `pages` depends on the worktree always being gone by then.
- Writing a cron line with both day-of-month and day-of-week restricted? Cron ORs them, not
  ANDs. Leave day-of-week as `*` and check it inside the command instead.
- Building a new "noisy by default" job that can get stuck failing repeatedly (merge
  conflicts, lock contention, etc)? Look at `gitops-deploy.sh`'s pause-file pattern before
  reaching for `--mode mute` — mute silences everything, the pause-file approach alerts once
  and then only stays quiet for that specific stuck state.
- Writing any new script that runs `git checkout`/`pull`/`push` against a shared repo? Take
  the `.git/sync.lock` first — see §9. Don't fall back to schedule-staggering.
