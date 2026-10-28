# Architecture & Design Notes

This file is for *why* things are wired the way they are — the anchor points I don't move,
the gotchas that aren't obvious until they bite, and the spots where two scripts quietly
depend on each other in a way that isn't visible from either file alone. The
[README](./README.md) is the runbook: what to run, in what order. This is the "why does this
actually work, and what breaks if I touch it" doc.

> ### Note on Implementation & AI Pairing
> The automation here prioritizes operational reliability over reinventing boilerplate. I leveraged AI pairing as an engineering accelerator to implement the Python utility daemons (e.g., `cron-guard.py` process tree handling and state hashing) and to tighten Bash error trapping, while driving the architectural constraints, process semantics, and failure boundaries myself.

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

The push at the end is explicit, and deliberately so. `backup-scripts-git.sh` runs `git push origin pages` for both repos, and skips it while a `dash-alarm-*` flag exists in the repo's `.git` (a known, already-alarmed problem such as a rebase conflict), so a stuck dashboard fails loudly once instead of every morning. Nothing else in the pipeline pushes any branch: `git-auto-sync.py` is no longer part of the daily run (see §18), so `pages` only moves because this script moves it. The `snapshots` branch uses the same technique: a temporary worktree, one commit, one push.

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

`git-auto-sync.py` is no longer part of the daily pipeline (see §18). It is kept for manual use, which is why the locking convention still applies to it.

Why `-n` / `LOCK_NB` instead of waiting: `gitops-deploy.sh` runs again in 15 minutes
regardless, so skipping one cycle and catching the deploy next time costs nothing. A
multi-second wait would also eat into whatever timeout `cron-guard` has set for that job, for
no upside.

Why the lock file specifically lives at `<repo>/.git/sync.lock`: it makes the convention apply
to any future script without needing a registry to remember it. The rule is just — touching
this repo's git state? Lock its own `.git/sync.lock` first.

---

## 10. GitOps Permission Tracking: Why `.git-perms` Exists

Git was designed for source code, not infrastructure state. It only tracks executable bits (`100755` vs `100644`) — it completely discards ownership (`chown`), group assignment, and restricted modes like `600` or `700`.

This creates silent failures during a **Fresh Start**:
* LinuxServer / S6-overlay containers (like Paperless-ngx) strictly require scripts in `custom-cont-init.d/` to be owned by `root:root` and executable. Git resets them to the cloning user, causing container startup routines to fail silently.
* Secrets and private configurations (like `docker-compose.env` or `acme.json`) get cloned with default umask permissions (`644`) instead of `600`.

### Why Not Hardcode Exceptions in `setup.sh`?
Hardcoding commands like `chown root:root /opt/stacks/paperless-ngx/...` into `setup.sh` creates stale technical debt. When a stack is deleted or modified, orphan fixup commands remain in the central provisioning script.

### The Solution: Self-Describing Repositories via `.git-perms`
Each repo maintains a lightweight, committed `.git-perms` file managed by `git-perms.sh`:
* **Scoped to tracked files only:** It reads `git ls-files`, ignoring transient media, databases, and logs.
* **Portable identity via `@USER:@GROUP`:** Files owned by the server's primary user are recorded as `@USER:@GROUP`. On restore, this dynamically resolves to whoever is running the bootstrap (e.g., `alice:alice` or `admin:admin`), preventing UID/GID mismatches across different Linux distros.
* **Preserves explicit system identities:** Any file owned by `root:root` or container-specific service UIDs (like `33:33` or `1000:1000`) is preserved verbatim.
* **Automated lifecycle (Exceptions Only):** `backup-scripts-git.sh` runs `git-perms.sh save` at 5:00 AM daily, but it only records genuine anomalies: files owned by `root:root` (or container service UIDs) and restricted permission modes (`600`, `700`, `750`). Standard files (`644` and `755` owned by `@USER:@GROUP`) are handled natively by Git and skipped. This guarantees that routine PRs adding or deleting normal files never dirty `.git-perms` on the server. During Fresh Start, `bootstrap.sh` restores exact permissions right after cloning.

---

## 11. Fresh Start SSH Architecture & Pre-Flight Traps

In a Full Recovery, `~/.ssh` and `/etc/ssh` are restored identically from the encrypted tarball. In a Fresh Start, you begin with a blank slate. Three non-obvious traps govern how `bootstrap.sh` handles SSH:

### 1. The Inbound vs. Outbound Separation
* **Inbound Access (Your Laptop $\rightarrow$ Server):** Handled purely by `~/.ssh/authorized_keys` containing your laptop's **public key**. The server does not need, and should never hold, your personal private keys.
* **Outbound Access (Server $\rightarrow$ Git):** Handled by a dedicated, single-purpose Git deploy private key. 
* **The Lockout Gate:** `bootstrap.sh` explicitly halts in Phase 1B if `~/.ssh/authorized_keys` is missing or empty. Because `setup.sh` installs a hardened SSH configuration that sets `PasswordAuthentication no`, running without `authorized_keys` guarantees permanent lockout.

### 2. The Non-Interactive `known_hosts` Freeze
On a fresh OS, `~/.ssh/known_hosts` does not exist. The first time `git clone git@github.com` runs, SSH prompts interactively: `Are you sure you want to continue connecting (yes/no)?`.
Because `bootstrap.sh` routes output through `quietly` (`>> "$LOGFILE" 2>&1`), there is no interactive terminal. The script will freeze indefinitely waiting for input.
* **The Fix:** Phase 2 runs `ssh-keyscan -H` for GitHub to seed `known_hosts` before any Git commands execute.

### 3. The `pipefail` Trap on `ssh -T`
Testing Git access via `ssh -T git@github.com` prints a success message ("*You've successfully authenticated*"), but **deliberately exits with status code `1`** because Git hosts do not provide an interactive shell.
Under `set -euo pipefail`, piping that directly (`ssh -T ... 2>&1 | grep ...`) causes Bash to treat the exit code `1` as a fatal pipeline failure, making the authentication test fail even when keys are completely valid.
* **The Fix:** Capture the output into a variable using `|| true` (`GIT_TEST=$(ssh -T ... 2>&1 || true)`) before evaluating with `grep`.

---

## 12. Known Trade-offs

`user_crontab.txt` is hand-wrapped through `cron-guard --mode fail`, but `cmd_setup_cron()`
would install a different, unwrapped line if I ran it. That's intentional, not a bug —
`cert-manager.sh` is the one script in the repo plausible to grab and run standalone,
independent of the rest of the cron-guard ecosystem, so it keeps its own self-contained
installer as a fallback for that case, even though the live deployment doesn't actually use
it.

**Rollback is a compose-file rollback, not a time machine.** It restores the last known good `compose.yml` only: not data, not other files in the stack folder, and not database migrations. Stacks where an older version could damage migrated data opt out with `deploy.rollback=false` and rely on alerting. After a rollback, Git and reality disagree (`main` still has the bad change) until the PR is reverted.

**The deploy checks prove "listening" and "answers HTTP", not "works".** An app can serve a login page with a broken backend. The baseline design also means containers that can't be probed (host network, no declared ports, unreachable from the host) only get the state and crash-loop checks. The IP-based probing relies on the host routing to Docker bridge networks, which holds on Linux but not on Docker Desktop.

---

## 13. Terminal AI Diagnostics (`oracle`): Failover, Sanitization & Pipe Gotchas

`oracle` acts as an on-demand terminal copilot for debugging container crashes, reviewing scripts, and querying system status via Google Gemini without leaving the shell.

### 1. In-Flight Secret Sanitization (Zero-Leakage Guarantee)
Piping terminal logs or configuration files directly to a cloud API carries a risk of leaking live credentials. 
* Before any payload is transmitted, `clean_piped_text()` executes an in-memory regex scrubbing pass that strips high-entropy tokens (`gsk_...`, `ghp_...`, `AIzaSy...`), private key blocks (`-----BEGIN PRIVATE KEY-----`), database connection URIs, and generic secret assignments (`password=`, `token=`).
* The sanitization occurs in RAM before the HTTP request is constructed, ensuring live secrets never leave the server.

### 2. The 503 High-Demand Failover Cascade
On Google AI Studio's free tier, standard flagship models (`gemini-3.5-flash`) frequently experience transient `503 Service Unavailable` spikes during peak hours when processing multi-thousand-token prompts (such as full 400-line scripts or container log dumps).
* To prevent terminal crashes, `stream_gemini()` implements a `MODEL_CASCADE` failover loop:
  `gemini-3.5-flash` > `gemini-flash-lite-latest` > `gemini-3.5-flash-lite`
* If a 503 occurs, the engine immediately switches to the high-concurrency `-lite` variant, which uses significantly less compute and almost never experiences load-shedding, streaming the answer without interrupting the user.

### 3. The Binary Pipe & Empty-Pipe Defense
* **Corrupted Logs:** In Linux, reading raw text from a pipe (`sys.stdin.read()`) crashes with a fatal `UnicodeDecodeError` if a crashed container spits out non-UTF8 bytes or binary memory dumps. `oracle` uses `sys.stdin.buffer.read().decode('utf-8', errors='replace')` to guarantee it never fails on dirty logs.
* **Empty Pipes:** If an inspected service has zero logs (e.g. `cat /dev/null | oracle`), standard CLI prompts hit an immediate `EOFError`. The script checks for whitespace-only input and exits cleanly with an informational notice.

---

## 14. Self-Documenting Headers: The `doc-injector` Pipeline

To keep the live dashboard accurate without manual documentation overhead, `doc-injector.py` analyzes modified scripts and injects structured `# @CAPABILITIES:` blocks directly into their headers before daily commits.

### 1. Decoupled from Production (Why It Was Removed from the 5 AM Run)
Earlier designs ran `doc-injector.py` during step G of the 5:00 AM server backup. This created two serious issues:
* **Byte-offset corruption:** Bash reads scripts in 4KB chunks using byte offsets. Rewriting a running script in place shifts byte boundaries mid-execution, causing syntax errors in downstream commands.
* **Dirty Git working tree:** Rewriting headers on production leaves uncommitted changes on `main`, tripping the daily drift monitor and violating the one-way GitOps flow.
* **The Rule:** `doc-injector.py` does not run on the production server. Documentation injection happens on the development machine before merging, or as an on-demand update. `IGNORE_FILES` permanently excludes `doc-injector.py` itself to prevent recursive self-modification.

### 2. Infinite Hash Recursion Prevention
To prevent `doc-injector.py` from constantly triggering AI requests on scripts that haven't functionally changed:
* `get_file_hash()` uses regex to strip out any existing `# @CAPABILITIES:` block *before* calculating the SHA-256 checksum.
* This ensures that updating the documentation header does not invalidate the file's code hash on subsequent runs. Relative paths are used in `.doc_hash_cache.json` so the cache remains portable across machines.

### 3. Pipeline Execution Sequence
Dashboards (`generate-dashboards.sh`) read the existing `# @CAPABILITIES:` headers directly from scripts when rendering the HTML UI. Because `doc-injector.py` runs upstream during development rather than during server backups, production remains a pure consumer: it reads headers to generate the dashboard without ever modifying executable code on disk.

---

## 15. CI/CD Architecture, Security & Pipeline Gotchas

The three ecosystem repositories (`homelab-blueprint`, `ctrl-s-master`, and `server-docker-backup`) are guarded by automated GitHub Actions pipelines designed with strict supply-chain and branch-protection constraints.

### 1. The Branch Protection "Expected" Deadlock (Why `paths` is Omitted on PRs)
When a GitHub Ruleset / Branch Protection rule marks a check (like `pytest` or `shellcheck`) as **"Required"**, GitHub will block the PR merge button until it receives a status report with that exact name.
* **The Gotcha:** If a workflow uses `paths: ['**.py']` and a PR only edits `README.md`, GitHub skips the workflow entirely. Because the workflow never runs, it never reports a status, leaving the PR indefinitely stuck on `Waiting for status to be reported (Expected)`.
* **The Rule:** Workflows running required branch checks must trigger on `pull_request` **without** path filters so they always report a passing status, even on documentation-only commits.

### 2. Full Commit SHA Pinning on GitHub Actions
Third-party Actions (such as `aquasecurity/trivy-action`) are pinned to immutable **40-character commit SHAs** rather than mutable tags (e.g., `@v0.36.0`):
```yaml
uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0
```

### 3. Compose Validation Needs a Stand-in `.env`
`docker compose config -q` refuses to render a file whose `${VAR}` references have no value, and the real `.env` files are (rightly) not in git. The CI job copies each stack's `.env.example` to `.env` for the duration of the check, validates, then deletes the copy. It proves syntax and schema only, not that images exist or the stack starts. A stack also fails validation if its `.env.example` leaves a *required* value blank: give such variables a default (`${PORT:-8080}`) or a sample value.

---

## 16. GitOps Deployer: Verify, Roll Back, Retry

Section 8 covers what happens when the *pull* fails. This section covers what happens when the *deploy* does.

**The flow, per changed stack** (only stacks that already have running containers; a stopped stack is left offline on purpose):
1. Record a **baseline** from the version that is running right now.
2. `docker compose pull` (3 attempts), then `up -d --wait` (default 120 s).
3. **Soak** for 30 s: restart counters must not move, and no container may be `restarting`, `dead`, `created`, unhealthy, or exited non-zero.
4. Compare against the baseline (below).
5. On success, copy that `compose.yml` to `.git/gitops_state/lastgood/<stack>.yml`. On failure, start the last known good file (`-f` it, with `--project-directory` pointing at the stack), then alert.

Why a *copy* of the compose file instead of `git revert`: the checkout has to stay equal to `origin/main`. Reverting locally would make the server diverge and the next pull conflict.

**The baseline is the important idea.** Before touching anything, the deployer reaches every running container by its IP on the docker network (the host can route to bridge networks, so nothing has to be published) and records which declared TCP ports accept connections and which of those answer HTTP(S) on `/` with a status below 500. After the update, every service that was listening must listen again and every service that answered HTTP must answer again, within `PROBE_TIMEOUT` (120 s). Only checks that passed *before* can fail the *update*, so a container that doesn't listen, isn't HTTP, was already returning 5xx, or can't be reached from the host is simply not gated. A wrong rollback is worse than a missed one, so the design leans toward under-checking. Database, mail, SSH and DNS ports are never sent HTTP.

**Optional labels** on any service in the stack's compose file: `deploy.rollback=false` for stacks with databases or migrations (still deployed, verified, alerted on and retried, never rolled back), and `deploy.healthcheck=http://<name>:<port>/<path>` for apps where `/` proves little. A health URL that doesn't answer on the running version is ignored with a warning, so a wrong path can't cause a rollback.

**Retry and alerting.** A stack that failed without a rollback (opt-out, no last-good file, failed pull) is written to `.git/gitops_retry` and retried every cycle, even though git is already up to date for it. The script exits non-zero only when the failed set *changes*, so cron-guard alerts once instead of every 15 minutes. A successful rollback also exits non-zero once; its Telegram message says that `main` still contains the bad change and the PR needs reverting.

**Helper modes.** `--verify STACK` runs the real checks against a live stack without deploying anything and prints its baseline. `--probe CONTAINER` finds a health path for the optional label. Neither takes the lock or writes state.

**Testing.** The logic is exercised with a mocked `docker` CLI pointed at real listening sockets and a real HTTP server: healthy deploys, a crash loop, a failed start, a service that stops listening, 5xx after the update, a slow starter, an already-broken service, a non-HTTP port, a failed rollback and a failed pull. `--verify` runs the same checks against real Docker.

---

## 17. Alert De-duplication in `cron-guard`

Jobs that run every few minutes and fail the same way every time used to send the same alert every time. `--repeat-after DURATION` stops that without hiding new problems.

* **Fingerprint:** a failure is its exit code plus the last lines of output with digits, hex IDs and ANSI codes stripped, so a changing timestamp or PID doesn't make the same error look new.
* **Same fingerprint:** silent until `DURATION` has passed since the last alert, then one reminder ("Still failing: N runs since …, M alerts suppressed"). **Different fingerprint:** alert immediately. `--recovery` adds one message on the first success after an alerted failure.
* **Why not "alert once until it succeeds"?** A second, different failure while the job is still failing would be invisible.
* **State** is a small JSON file per job in `state/`, written atomically. It **fails open**: an unreadable or damaged file, or a failed Telegram delivery, makes the failure count as new and alert. A suppression bug must never be able to hide an outage. (A damaged state file once crashed the wrapper before that fallback existed.)
* Off by default: jobs without the flags behave exactly as before. Because digits are ignored, two errors that differ only by a number count as the same.

---

## 18. Branch Model: Humans Use PRs, Machines Use Side Branches

`main` in all three repos is protected by a ruleset (PR required, required checks, no force-push). Nothing automated pushes to it. `backup-scripts-git.sh` used to run `git-auto-sync.py` over every repo each morning, which meant the server's working tree could reach `main` (and, for `/opt/stacks`, production, since the deployer pulls `main`) without review or CI.

What the 5 AM job does now:
* **Snapshots** (crontabs, package lists, dotfiles, APT sources, sshd drop-ins) are copied into a temporary worktree of the unprotected `snapshots` branch, using an allow-list `.gitignore` (only `run_once/system_configs` and `run_once/dotfiles`; `.env`, `*.pem` and `*.key` always excluded). The job commits only when something changed, then pushes. It is the same technique as the dashboards: a throwaway worktree shares the object database, so the main working tree is never disturbed. `bootstrap.sh` restores these files during Fresh Start.
* **Dashboards** go to `pages`, as in §6.
* **Drift report:** `git status --porcelain` on the three repos; it never commits. A new set of uncommitted changes exits non-zero once (so cron-guard alerts) and at most weekly while it persists (the hash includes the ISO week). Real changes go through a branch and a PR.

Consequences: the server consumes `main` (the deployer pulls it), the only things it publishes are generated files on side branches, and no job needs a bypass for direct pushes. Anything that lands on the server without a PR appears in the drift report instead of being pushed silently.

---

## Quick Reference: Gotchas

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
- Touching `generate-dashboards.sh`'s cleanup trap? Don't remove it. Git refuses to check out a branch that is already checked out in another worktree, so a leaked worktree breaks the next run's handling of `pages` and `snapshots`.
- Writing a cron line with both day-of-month and day-of-week restricted? Cron ORs them, not
  ANDs. Leave day-of-week as `*` and check it inside the command instead.
- Building a new "noisy by default" job that can get stuck failing repeatedly (merge
  conflicts, lock contention, etc)? Look at `gitops-deploy.sh`'s pause-file pattern before
  reaching for `--mode mute` — mute silences everything, the pause-file approach alerts once
  and then only stays quiet for that specific stuck state.
- Writing any new script that runs `git checkout`/`pull`/`push` against a shared repo? Take
  the `.git/sync.lock` first — see §9. Don't fall back to schedule-staggering.
- Adding custom container init scripts (S6-overlay)? Check their permissions in `.git-perms` — scripts in `custom-cont-init.d` must be `755 root:root`.
- Cloning Git repos inside automated scripts? Pre-seed `known_hosts` with `ssh-keyscan` first, or the script will hang indefinitely on the host verification prompt.
- Testing SSH authentication via `ssh -T` under `pipefail`? Always append `|| true` to the subshell command; Git hosts exit with code 1 by design, which will trip `set -o pipefail`.
- Setting up a Fresh Start? Never skip `~/.ssh/authorized_keys`; `setup.sh` disables password authentication, so missing client public keys will lock you out.
- Piping Docker logs to `oracle`? Always use `2>&1` (e.g. `docker logs container 2>&1 | oracle`) — Docker routes runtime application logs to stderr, which will bypass a standard `|` pipe unless merged.
- `oracle` hitting Google 503 capacity errors? It auto-fails over to `-lite` models automatically, but you can set `GEMINI_MODEL='gemini-flash-lite-latest'` in `.env` to make the high-capacity model default.
- Running `doc-injector.py`? Run it on your development machine before committing; it was decoupled from the 5 AM server backup so production never mutates executable scripts on disk.
- Wondering why `doc-injector.py` isn't re-analyzing a script? It checks code hashes excluding the `@CAPABILITIES` block — use `--force` to bypass the cache.
- Setting up a required PR check in GitHub Actions? Never combine it with `paths` filtering, or documentation-only PRs will freeze in an endless `Expected` status check lock. See §15.1.
- Adding third-party GitHub Actions? Pin to full 40-character commit SHAs, never mutable tags or `latest`. See §15.2.
- Testing Compose files in CI? Ensure `.env.example` exists; the CI validator copies it temporarily so Docker Compose can resolve required variable references. See §15.3.
- Adding a stack with a database or one-way migrations? Add `deploy.rollback=false` to any service in its compose file, or a failed update will be rolled back to an older app version on a newer schema. See §16.
- Wondering what the deployer actually checks for a stack? Run `gitops-deploy.sh --verify <stack>`: it prints the baseline (ports and HTTP endpoints) and runs the checks without deploying. See §16.
- A cron job failing the same way every few minutes? Add `--repeat-after 6h` (and `--recovery`) to its cron-guard line instead of muting it. See §17.
- Need to publish a generated file from the server? Don't push `main`. Use a temporary worktree on a side branch like `pages` and `snapshots`. See §18.
