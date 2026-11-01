# Architecture & Design Notes

This file explains *why* things are set up the way they are: what I never move, what bites you if you forget it, and where two scripts quietly depend on each other.

The [README](./README.md) tells you what to run and in what order. This file tells you why it works and what breaks if you change it.

> **A note on AI help**
> I used AI as a speed-up for some of the code: the Python helper scripts (like `cron-guard.py`'s process handling and state hashing) and tighter error handling in Bash. The design decisions, the rules for how processes behave, and the choices about what's allowed to fail were mine.

---

## 1. Three Paths I Never Rename

| Path | What it is | Set in |
|---|---|---|
| `/opt/rabbit-hole` | Symlink to my real scripts folder (`$HOME/scripts`) | `setup.sh` |
| `/opt/venv` | The Python virtualenv every script uses | `setup.sh` |
| `/opt/stacks` | Docker Compose stacks (separate repo) | `setup.sh` / `bootstrap.sh` |

The only thing allowed to move is `MY_SCRIPTS="$HOME/scripts"` in `setup.sh`. That's where the real files live, and `/opt/rabbit-hole` is just a nickname for it. If the real folder ever moves, that's the one line to change.

Renaming `/opt/rabbit-hole` itself is a bigger job. Every script that hardcodes it has to change, including `setup.sh`, which both creates the symlink and uses it (cron-guard symlink, `.env` checks, systemd `ExecStart`).

If I ever do that rename, only search for the exact text `/opt/rabbit-hole`. Never touch plain `$HOME` or `$HOME/scripts`. They show up everywhere for unrelated reasons (dotfiles, `.ssh`, mkcert), and a blind find-and-replace would break them.

---

## 2. DNS: My Own Server Has to Be First

```json
"dns": ["${SERVER_IP}", "8.8.8.8", "1.1.1.1"],
```

This order in `daemon.json` matters, and it exists because I got burned.

My domain is private. It's signed by mkcert and not registered anywhere public. Google and Cloudflare don't know it exists. Only my own Unbound resolver (`setup.sh`, step 7/10) can answer for it.

Containers don't just serve that domain, they also talk to each other through it. Nextcloud and n8n log in through Pocket ID using OIDC. For that, the container has to look up the login server's hostname itself, to reach its discovery endpoint and check redirect addresses. If the first DNS server can't answer for my private domain, the lookup fails (or lands on something that isn't Pocket ID), and login breaks with errors that don't mention DNS at all.

That's what happened. I rebuilt `daemon.json` by hand, put Google and Cloudflare first because it felt like the safe choice, and OIDC broke in every container that used it. It took a while to connect "login is broken" to "the DNS order changed."

This setup is called split-horizon DNS, and it's the normal way to run an internal-only domain. It's easy to break by accident, though: putting a public resolver first while debugging something else will quietly break every OIDC container.

**Rule:** `SERVER_IP` stays first. If Docker DNS acts up, check the order before anything else.

---

## 3. The Symlink Ownership Trap

`/opt/rabbit-hole` is made with `sudo ln -sf "$MY_SCRIPTS" /opt/rabbit-hole`, so the link itself is owned by root, even though the folder it points to is mine.

`stat` doesn't follow symlinks by default (same as `ls -l`), so checking the bare link gives you root:

```bash
stat -c '%U' /opt/rabbit-hole       # → root  (the link's own owner, wrong)
stat -c '%U' /opt/rabbit-hole/.     # → you   (trailing dot follows the link, right)
stat -L -c '%U' /opt/rabbit-hole    # → you   (explicit flag, clearest to read)
```

This only matters when the symlink is the *last* part of the path. Anything reached *through* it, like `/opt/rabbit-hole/some_file` or `${BASH_SOURCE[0]}` when a script is run as `/opt/rabbit-hole/script.sh`, is resolved automatically. That's why `local-opt-backup.sh` (`stat -c '%U' "${BASH_SOURCE[0]}"`) and `vergil.py` (`Path(__file__).resolve().owner()`) work fine: the file isn't a symlink, only a folder earlier in the path is.

The one place it really matters is `task_nextcloud()` in `container-watcher.sh`. It uses `stat -L -c '%U' /opt/rabbit-hole` to find the real owner before a `sudo -u` call. Without `-L`, it would quietly run as root.

---

## 4. Disaster Recovery: Numbers, Not Names

Restore doesn't trust usernames. It uses numeric IDs, and tracks UID and GID separately because they don't always match up.

**At backup** (`local-opt-backup.sh`):
```bash
SCRIPT_OWNER=$(stat -c '%U' "${BASH_SOURCE[0]}")
echo "$(id -u "$SCRIPT_OWNER"):$(id -g "$SCRIPT_OWNER")" > /tmp/backup-uid.txt
```
That pair of numbers goes inside the encrypted archive.

**At restore** (`bootstrap.sh`):
```bash
IFS=: read -r B_UID B_GID < /tmp/backup-uid.txt
sudo find "$DIR_STACKS" "$DIR_SCRIPTS" "$DIR_CTRL" "$HOME/.ssh" \
    \( -uid "$B_UID" -o -gid "$B_GID" \) \
    ! \( -uid "$(id -u)" -a -gid "$(id -g)" \) \
    -exec chown "$(id -u):$(id -g)" {} +
```

Why both numbers: a new machine usually gives the first user UID 1000 again, but the GID can differ if another group already took that number. Checking only UID would miss that. So I look for anything still carrying *either* old number and fix it unless it's already correct on both.

It only touches a few private folders, so it can't reach things that rightfully belong to someone else, like container users (`www-data:33`).

---

## 5. `cron-guard --mode mute`: Started as a Label, Became Useful

`mute` started as a labeling trick.

`homelab_dash.py` builds the dashboard from crontab comments and command text. A cron line that doesn't go through `cron-guard` only gets whatever comment is above it, cut short and guessed at. Wrapping a job as `cron-guard --mode <mode> "Job Name" ...` (any mode) gives the dashboard something clean to read:

```python
cg_match = re.search(r'cron-guard(?:\.py)?\s+--mode\s+(?:fail|success|all|mute)\s+["\']([^"\']+)["\']', cmd)
```

So `mute` first existed so quiet jobs (no Telegram messages) could still show up properly on the dashboard.

It later got a real second job. In `mute` mode, every run writes `status/<job>_<hash>.json` with the last run time, exit code, duration, and whether it timed out. This file stays local. Nothing sends it anywhere and the dashboard doesn't read it. It's the only way to check by hand that a permanently silent job really ran, since nothing else will ever tell you.

---

## 6. The Dashboard Pipeline

Every day at 5am, `backup-scripts-git.sh` runs `generate-dashboards.sh`. The problem it solves: how do I commit new HTML to the `pages` branch without disturbing the main folder, which might have real unfinished work in it?

The answer is a temporary git worktree instead of switching branches:
```bash
git -C "$CURRENT_REPO" worktree add "$CURRENT_WORKTREE" pages
git -C "$CURRENT_WORKTREE" pull --rebase origin pages
cp "$temp_html" "$CURRENT_WORKTREE/index.html"
```
A worktree is a second checkout that shares the same git data. Commits made there are real commits on `pages`, but the main folder's branch and uncommitted changes are never touched. A `trap ... EXIT INT TERM` always removes the worktree (`rm -rf` + `worktree prune`), however the script ends.

There's also a noise problem. Every dashboard has a `Generated <timestamp>` line that changes on every run. Committing that daily would bury real changes. So:
```bash
git diff --cached --quiet -I "[Gg]enerated "
```
`git diff -I<regex>` ignores a change if every changed line matches the pattern. On a day where only the timestamp changed, it sees "nothing real changed" and skips the commit.

The push at the end is deliberate. `backup-scripts-git.sh` runs `git push origin pages` for both repos. It skips the push while a `dash-alarm-*` flag exists in the repo's `.git` (a known problem that already alerted, like a rebase conflict), so a stuck dashboard fails loudly once instead of every morning. Nothing else pushes any branch. `git-auto-sync.py` is no longer in the daily run (see §18), so `pages` only moves because this script moves it. The `snapshots` branch uses the same trick: temporary worktree, one commit, one push.

One thing depends on this: the worktree has to be gone before anything else tries to check out `pages` in the main folder. Git won't let one branch be checked out in two places. The trap is what guarantees that. If it were ever skipped or broken, the next attempt would fail with "already checked out elsewhere."

---

## 7. Cron's "Day of Month OR Day of Week" Trap

```
0 2 8-14,22-28 * * [ "$(date +\%u)" = 5 ] && cron-guard --mode fail "ctrl_s_master" "$CTRL_DIR/run.sh"
```

This should run on the 2nd and 4th Friday of the month. The obvious way, `0 2 8-14,22-28 * 5 ...`, doesn't work. When both day-of-month and day-of-week are set, cron runs the job if *either* matches, not both. That version would fire on days 8–14 and 22–28, *plus* every Friday.

The fix is to leave day-of-week as `*` so cron only filters by date, then check the weekday inside the command with `date +%u` and `&&`. That gives a true "and."

`homelab_dash.py` has a regex that spots the `date +\%[uwaA] ... &&` pattern and shows it as something readable ("2nd and 4th Friday") instead of raw cron text. It isn't tied to this one line, so any future odd schedule written this way gets the same treatment.

---

## 8. GitOps Conflicts: Alert Once, Then Go Quiet

`gitops-deploy.sh` runs every 15 minutes and needed its own way to avoid alert spam, separate from `mute`. When a merge conflict happens during `git pull --rebase --autostash`:

```bash
if ! git pull -q origin main --rebase --autostash; then
    if [ ! -f "$PAUSE_FILE" ]; then
        send_telegram "🚨 GitOps Paused: Merge Conflict Detected..."
        touch "$PAUSE_FILE"
    fi
    exit 0   # not exit 1 — deliberately suppresses cron-guard's --mode fail alert
fi
```

If I let `--mode fail` handle it, the same conflict would send a Telegram alert every 15 minutes until I fixed it. `mute` exists to prevent that kind of noise, but this job isn't silent by default. It alerts on failure by default and needs to quiet *itself* after the first alert.

So: send one detailed alert, drop a marker file, then exit cleanly on every later run so cron-guard sees "success" and stays quiet. The alert also contains the recovery steps: either `git restore .` to throw away the conflicting local change, or stage and commit it properly, then do a normal push. The next clean pull clears `$PAUSE_FILE` by itself and alerting goes back to normal.

---

## 9. Repo Locking Rule

Any script that changes a repo's working folder (`checkout`, `pull`, `push`) must first take a non-blocking `flock` on that repo's own `.git/sync.lock`, and release it when done.

This replaced my earlier approach of spacing out the schedules. `gitops-deploy.sh` (every 15 min) and the daily backup sync both touched `$STACKS_DIR` with no coordination, and once ran at the exact same minute. Spacing them out lowered the odds of a clash. Locking removes it.

**Bash** (`gitops-deploy.sh`):
```bash
exec 200>"${STACKS_DIR}/.git/sync.lock"
flock -n 200 || { echo "⏭️  Stacks repo busy, skipping this cycle."; exit 0; }
```
The lock is released automatically when the script ends.

**Python** (`git-auto-sync.py`): same idea. It uses `fcntl.flock(..., LOCK_EX | LOCK_NB)` on Linux and `msvcrt.locking()` on Windows, chosen once by `sys.platform` (same pattern as the OS-specific process kill in `cron-guard.py`). It takes the lock right after `chdir` into the repo and holds it with `try`/`finally`, so it's released on every exit, including `sys.exit()`.

`git-auto-sync.py` is no longer part of the daily run (see §18). I keep it for manual use, so the lock rule still applies.

**Why don't I wait for the lock?** `gitops-deploy.sh` runs again in 15 minutes anyway, so skipping one round costs nothing. Waiting would also eat into `cron-guard`'s timeout for that job, for no gain.

**Why `<repo>/.git/sync.lock`?** The rule then applies to any future script without needing a list to remember: touching this repo's git state? Lock its own `.git/sync.lock` first.

---

## 10. Why `.git-perms` Exists

Git was built for source code. It only remembers the executable bit (`100755` vs `100644`). It forgets who owns a file, its group, and strict modes like `600` or `700`.

That causes quiet failures on a **Fresh Start**:
* LinuxServer / S6-overlay containers (like Paperless-ngx) require scripts in `custom-cont-init.d/` to be `root:root` and executable. After a clone they belong to whoever cloned, and the container's startup steps fail silently.
* Secret files (`docker-compose.env`, `acme.json`) come back with normal permissions (`644`) instead of `600`.

**Why not hardcode fixes in `setup.sh`?** Lines like `chown root:root /opt/stacks/paperless-ngx/...` go stale. When a stack is removed or changed, the leftover lines stay in the main setup script.

**What I do instead:** each repo carries a small committed `.git-perms` file, managed by `git-perms.sh`:
* **Tracked files only.** It reads `git ls-files`, so it ignores media, databases, and logs.
* **Portable owner.** Files owned by the server's main user are saved as `@USER:@GROUP`. On restore this becomes whoever runs the bootstrap (e.g. `alice:alice` or `admin:admin`), so different Linux setups don't clash.
* **System owners are kept exactly.** Files owned by `root:root` or container user IDs (like `33:33` or `1000:1000`) are saved as they are.
* **Exceptions only.** `backup-scripts-git.sh` runs `git-perms.sh save` at 5:00 AM daily, but records only the unusual: `root:root` (or container-user) files and restricted modes (`600`, `700`, `750`). Normal files (`644` and `755`, owned by `@USER:@GROUP`) are left to Git. So ordinary PRs that add or delete files never make `.git-perms` dirty on the server. During Fresh Start, `bootstrap.sh` restores the exact permissions right after cloning.

---

## 11. Fresh Start: SSH Traps

In a Full Recovery, `~/.ssh` and `/etc/ssh` come back exactly from the encrypted archive. A Fresh Start begins from nothing, and `bootstrap.sh` handles three non-obvious SSH problems.

### 1. Inbound and outbound access are separate
* **Inbound (laptop → server):** only needs your laptop's **public key** in `~/.ssh/authorized_keys`. The server should never hold your personal private keys.
* **Outbound (server → Git):** uses a separate deploy private key, used only for that.
* **The lockout gate:** `bootstrap.sh` stops in Phase 1B if `~/.ssh/authorized_keys` is missing or empty. `setup.sh` sets `PasswordAuthentication no`, so without that file you would be locked out for good.

### 2. `known_hosts` freezes the script
On a fresh OS there's no `~/.ssh/known_hosts`. The first `git clone git@github.com` asks "Are you sure you want to continue connecting (yes/no)?". `bootstrap.sh` sends its output through `quietly` (`>> "$LOGFILE" 2>&1`), so nobody can answer, and the script hangs forever.
* **Fix:** Phase 2 runs `ssh-keyscan -H` for GitHub first, so `known_hosts` is filled in before any Git command.

### 3. `ssh -T` fails under `pipefail`
`ssh -T git@github.com` prints "You've successfully authenticated" but **exits with code `1` on purpose**, since Git hosts give no shell. Under `set -euo pipefail`, piping it straight (`ssh -T ... 2>&1 | grep ...`) counts that `1` as a failure, so the test fails even when your keys are fine.
* **Fix:** save the output first with `|| true` (`GIT_TEST=$(ssh -T ... 2>&1 || true)`), then run `grep` on it.

---

## 12. Known Trade-offs

**`cert-manager.sh` has its own installer.** `user_crontab.txt` is hand-wrapped with `cron-guard --mode fail`, but `cmd_setup_cron()` would install a different, unwrapped line if I ran it. That's on purpose. `cert-manager.sh` is the one script I might grab and run on its own, without the rest of cron-guard, so it keeps its own installer as a fallback, even though my real setup doesn't use it.

**Rollback only restores the compose file.** It brings back the last known good `compose.yml`, and nothing else: not data, not other files in the stack folder, not database migrations. Stacks where an older version could damage migrated data opt out with `deploy.rollback=false` and rely on alerts. After a rollback, Git and reality disagree (`main` still has the bad change) until the PR is reverted.

**The deploy checks prove "listening" and "answers HTTP", not "works".** An app can show a login page with a broken backend. Containers that can't be probed (host network, no declared ports, not reachable from the host) only get the state and crash-loop checks. The IP-based probing relies on the host being able to reach Docker bridge networks. That works on Linux but not on Docker Desktop.

---

## 13. `oracle`: Terminal AI Help

`oracle` is a terminal helper for debugging container crashes, reviewing scripts, and asking about system status through Google Gemini, without leaving the shell.

### 1. Secrets are scrubbed before anything is sent
Sending logs or configs to a cloud API could leak real credentials. Before any request is built, `clean_piped_text()` scrubs the text in memory. It removes high-entropy tokens (`gsk_...`, `ghp_...`, `AIzaSy...`), private key blocks (`-----BEGIN PRIVATE KEY-----`), database connection URIs, and generic secret assignments (`password=`, `token=`). This happens in RAM before the HTTP request exists, so live secrets never leave the server.

### 2. Fallback when Google is overloaded (503)
On Google AI Studio's free tier, the main models (`gemini-3.5-flash`) often return `503 Service Unavailable` at busy times, especially with big prompts (a 400-line script or a log dump). So `stream_gemini()` has a `MODEL_CASCADE` fallback:
`gemini-3.5-flash` > `gemini-flash-lite-latest` > `gemini-3.5-flash-lite`
On a 503 it switches straight to the lighter `-lite` model, which rarely gets turned away, and keeps streaming the answer.

### 3. Bad input from pipes
* **Broken logs:** a crashed container can output bytes that aren't valid UTF-8, and `sys.stdin.read()` would crash with `UnicodeDecodeError`. `oracle` reads with `sys.stdin.buffer.read().decode('utf-8', errors='replace')`, so messy logs never break it.
* **Empty pipes:** if a service has no logs (`cat /dev/null | oracle`), a normal prompt hits `EOFError` right away. `oracle` checks for empty or whitespace-only input and exits with a short notice.

---

## 14. `doc-injector`: Scripts That Describe Themselves

To keep the dashboard accurate without writing docs by hand, `doc-injector.py` looks at changed scripts and adds a `# @CAPABILITIES:` block to their headers before the daily commit.

### 1. Why it no longer runs on the server
It used to run in step G of the 5:00 AM backup, which caused two problems:
* **Broken running scripts:** Bash reads scripts in 4KB chunks by byte position. Rewriting a script while it runs shifts those positions and causes syntax errors in later commands.
* **Dirty working tree:** Changing headers on production leaves uncommitted edits on `main`. That trips the daily drift check and breaks the one-way GitOps flow.
* **Rule:** `doc-injector.py` never runs on the production server. I run it on my dev machine before committing, or on demand. `IGNORE_FILES` always excludes `doc-injector.py` itself so it can't edit itself.

### 2. No endless re-checking
So it doesn't keep calling the AI for scripts that haven't really changed:
* `get_file_hash()` removes any existing `# @CAPABILITIES:` block with a regex *before* making the SHA-256 hash. Updating the header therefore doesn't change the file's code hash on the next run. The cache `.doc_hash_cache.json` uses relative paths so it works on any machine.

### 3. How the pieces fit
`generate-dashboards.sh` reads the existing `# @CAPABILITIES:` headers when it builds the HTML. Since `doc-injector.py` runs earlier on my dev machine, production only reads headers and never edits scripts.

---

## 15. CI/CD: Rules and Gotchas

The repos `homelab-blueprint`, `ctrl-s-master`, and `server-docker-backup` are protected by GitHub Actions, built with strict supply-chain and branch-protection rules.

### 1. Why PR checks have no `paths` filter
When a branch protection rule marks a check (like `pytest` or `shellcheck`) as **Required**, GitHub blocks the merge button until that exact check reports a status.
* **The problem:** if a workflow has `paths: ['**.py']` and a PR only edits `README.md`, GitHub skips the workflow. It never reports, so the PR sits forever at `Waiting for status to be reported (Expected)`.
* **Rule:** workflows for required checks must run on `pull_request` **without** path filters, so they always report a pass, even on docs-only changes. (Targeted workflows like `terraform.yml` do use path filters because they only care about infrastructure files.)

### 2. Pin third-party Actions to full commit SHAs
Third-party Actions (like `aquasecurity/trivy-action`) use an unchangeable **40-character commit SHA**, not a tag like `@v0.36.0` that can be moved:
```yaml
uses: aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25 # v0.36.0
```

### 3. Compose validation needs a fake `.env`
`docker compose config -q` refuses to render a file when a `${VAR}` has no value, and real `.env` files are (rightly) not in git. So CI copies each stack's `.env.example` to `.env`, validates, then deletes it. This proves the syntax and schema are fine. It does not prove the images exist or the stack starts. A stack also fails validation if `.env.example` leaves a *required* value blank. Give such variables a default (`${PORT:-8080}`) or a sample value.

---

## 16. GitOps Deployer: Verify, Roll Back, Retry

Section 8 covers a failed *pull*. This section covers a failed *deploy*.

**Steps for each changed stack** (only stacks that already have running containers; a stopped stack stays off on purpose):
1. Record a **baseline** from the version running right now.
2. `docker compose pull` (3 tries), then `up -d --wait` (default 120 s).
3. **Soak** for 30 s. Restart counters must not increase, and no container may be `restarting`, `dead`, `created`, unhealthy, or exited with an error.
4. Compare with the baseline (below).
5. On success, copy that `compose.yml` to `.git/gitops_state/lastgood/<stack>.yml`. On failure, start the last known good file (`-f`, with `--project-directory` pointing at the stack), then send an alert.

**Why copy the compose file instead of `git revert`?** The checkout must stay equal to `origin/main`. A local revert would make the server drift from `main`, and the next pull would conflict.

**The baseline is the key idea.** Before changing anything, the deployer connects to every running container by its IP on the Docker network (the host can reach bridge networks, so nothing needs to be published). It records which declared TCP ports accept connections, and which of those answer HTTP(S) on `/` with a status below 500. After the update, every service that was listening must listen again, and every service that answered HTTP must answer again, within `PROBE_TIMEOUT` (120 s).

Only checks that passed *before* can fail the *update*. A container that wasn't listening, wasn't HTTP, was already returning 5xx, or couldn't be reached from the host is simply not checked. A wrong rollback is worse than a missed one, so I lean toward checking too little. Database, mail, SSH and DNS ports never get HTTP requests.

**Optional labels** on any service in a stack's compose file:
* `deploy.rollback=false` for stacks with databases or migrations. They are still deployed, checked, alerted on, and retried, but never rolled back.
* `deploy.healthcheck=http://<name>:<port>/<path>` for apps where `/` proves little. If that URL doesn't answer on the running version, it's ignored with a warning, so a wrong path can't cause a rollback.

**Retry and alerts.** A stack that failed without a rollback (opted out, no last-good file, or failed pull) is written to `.git/gitops_retry` and retried every cycle, even though git is already up to date for it. The script exits non-zero only when the set of failed stacks *changes*, so cron-guard alerts once instead of every 15 minutes. A successful rollback also exits non-zero once. Its Telegram message says `main` still has the bad change and the PR needs reverting.

**Helper modes.**
* `--verify STACK` runs the real checks on a live stack and prints its baseline, without deploying.
* `--probe CONTAINER` finds a health path for the optional label.
* Neither takes the lock or writes state.

**Testing.** The logic is tested with a fake `docker` command pointed at real listening sockets and a real HTTP server. The cases: healthy deploys, a crash loop, a failed start, a service that stops listening, 5xx after the update, a slow starter, an already-broken service, a non-HTTP port, a failed rollback, and a failed pull. `--verify` runs the same checks against real Docker.

---

## 17. Not Repeating the Same Alert (`cron-guard`)

Jobs that run every few minutes and fail the same way each time used to send the same alert every time. `--repeat-after DURATION` stops that without hiding new problems.

* **What counts as "the same failure":** the exit code plus the last lines of output, with digits, hex IDs and ANSI color codes removed. A changing timestamp or PID doesn't make the same error look new.
* **Same failure:** silent until `DURATION` has passed since the last alert, then one reminder ("Still failing: N runs since …, M alerts suppressed"). **Different failure:** alerts right away. `--recovery` adds one message on the first success after an alerted failure.
* **Why not "alert once until it succeeds"?** A second, different failure while the job is still failing would go unseen.
* **State** is a small JSON file per job in `state/`, written safely (atomically). It **fails open**: if the file is unreadable or damaged, or Telegram delivery failed, the failure counts as new and alerts. A bug in the quiet-down logic must never be able to hide an outage. (A damaged state file once crashed the wrapper before that fallback existed.)
* It's off by default. Jobs without the flags behave exactly as before. Since digits are ignored, two errors that differ only by a number count as the same.

---

## 18. Branch Model: People Use PRs, Machines Use Side Branches

`main` in all three repos is protected by a ruleset (PR required, required checks, no force-push). Nothing automated pushes to it.

Before, `backup-scripts-git.sh` ran `git-auto-sync.py` over every repo each morning. That meant the server's working folder could reach `main` with no review or CI. For `/opt/stacks` that also meant reaching production, since the deployer pulls `main`.

What the 5 AM job does now:
* **Snapshots** (crontabs, package lists, dotfiles, APT sources, sshd drop-ins) are copied into a temporary worktree of the unprotected `snapshots` branch. An allow-list `.gitignore` only lets through `run_once/system_configs` and `run_once/dotfiles`, and always excludes `.env`, `*.pem` and `*.key`. It commits only if something changed, then pushes. It's the same technique as the dashboards: a throwaway worktree shares the git data, so the main folder is never disturbed. `bootstrap.sh` restores these files during Fresh Start.
* **Dashboards** go to `pages`, as in §6.
* **Drift report:** `git status --porcelain` on the three repos. It never commits. A new set of uncommitted changes exits non-zero once (so cron-guard alerts), then at most weekly while it lasts (the hash includes the ISO week). Real changes go through a branch and a PR.

Result: the server only *reads* `main` (the deployer pulls it), the only things it publishes are generated files on side branches, and no job needs a bypass to push directly. Anything that reaches the server without a PR shows up in the drift report instead of being pushed silently.

---

## 19. Infrastructure as Code: GitHub Settings & Remote State

The settings for the three repos (`homelab-blueprint`, `ctrl-s-master`, `server-docker-backup`) are managed with Terraform/OpenTofu, so all three follow the same standards and drift gets caught.

### 1. One control repo
Instead of spreading Terraform across every repo, everything lives in `homelab-blueprint/terraform/`:
* **Split of jobs:** each app repo holds its own code and CI. The control repo manages repo-level rules: visibility, topics, merge policy, issue tracking, security alerts.
* **Reusable module:** repos are declared through one local module (`modules/github_repo`) with a firm baseline: `delete_branch_on_merge = true`, `allow_merge_commit = false` (squash merges only), and Dependabot vulnerability alerts on by default.

### 2. State stored in Backblaze B2 (S3-compatible)
So state isn't lost with one machine and CI runners can check changes, Terraform state lives in an encrypted Backblaze B2 bucket using the built-in `s3` backend.
* **Chicken-and-egg rule:** Terraform can't store state in a bucket it manages. I created the state bucket (`gravi-ctrl-tf-state`) by hand once in the provider console, with default server-side encryption (`SSE-B2`) and private access.
* **Flags needed because it isn't real AWS:**
  * `endpoints = { s3 = "https://..." }`: must include the `https://` part.
  * `use_path_style = true`: uses `https://endpoint/bucket` paths instead of subdomains, avoiding TLS wildcard certificate mismatches.
  * `skip_credentials_validation`, `skip_region_validation`, `skip_metadata_api_check`, `skip_requesting_account_id`: turns off AWS STS checks, the EC2 metadata lookup (`169.254.169.254`), and AWS account ID lookups.
  * `region = "us-east-1"`: a dummy value that satisfies the Go SDK's validation while the region checks are skipped.

### 3. Protection against accidents, and the `null` trap
* **Delete lock:** in `modules/github_repo/main.tf`, the main repo resource has `lifecycle { prevent_destroy = true }`. Even a typo or a stray run that tries to remove a repo stops before any delete call is sent.
* **The `null` trap:** in Terraform, leaving out an optional setting or setting it to `null` means "make the remote value empty." When adopting existing repos with `terraform import`, I must pass current values (like `description` or `homepage_url`) into the module call, or Terraform will plan to wipe them (`~ attribute = "..." => null`).

### 4. Terraform changes go through PRs
Terraform is never applied from my workstation:
1. Pushing a branch and opening a PR against `main` triggers `.github/workflows/terraform.yml`.
2. The workflow checks formatting (`terraform fmt -check`), connects to the Backblaze state, and runs `terraform plan -no-color`.
3. A bot posts the full plan, in an expandable comment, on the PR (via `actions/github-script`).
4. A reviewer checks the planned changes in the PR. Merging to `main` runs `terraform apply -auto-approve` on the runner, so real infrastructure always matches git history.

---

## Quick Reference: Gotchas

**DNS and paths**
- Docker DNS acting weird, or OIDC breaking across containers? Check `daemon.json`. `SERVER_IP` must be first, not Google or Cloudflare. (§2)
- Checking who owns `/opt/rabbit-hole`? Use `stat -L` or a trailing `/.`. The bare form returns root. (§3)
- Renaming `/opt/rabbit-hole`? Replace that exact text everywhere, `setup.sh` included. Never touch `$HOME` or `$HOME/scripts`. (§1)

**Backup, restore, permissions**
- Fixing ownership after a disaster recovery? Check UID *and* GID. They can drift separately. (§4)
- Adding custom container init scripts (S6-overlay)? Check `.git-perms`. Scripts in `custom-cont-init.d` must be `755 root:root`. (§10)

**Fresh Start and SSH**
- Cloning Git repos in scripts? Seed `known_hosts` with `ssh-keyscan` first, or the script hangs on the host prompt. (§11)
- Testing with `ssh -T` under `pipefail`? Add `|| true`. Git hosts exit with 1 on purpose. (§11)
- Setting up a Fresh Start? Never skip `~/.ssh/authorized_keys`. `setup.sh` turns off password login, so you'd be locked out. (§11)

**Cron and alerts**
- Always-silent cron job? Use `cron-guard --mode mute`. You get a clean dashboard label and a heartbeat file. (§5)
- Cron line with both day-of-month and day-of-week? Cron ORs them. Leave day-of-week as `*` and check it inside the command. (§7)
- New job that's noisy by default and can get stuck failing (merge conflicts, lock contention)? Look at the pause-file pattern in `gitops-deploy.sh` before using `--mode mute`. Mute silences everything. The pause file alerts once, then only stays quiet for that one stuck state. (§8)
- Job failing the same way every few minutes? Add `--repeat-after 6h` (and `--recovery`) instead of muting it. (§17)

**Git and branches**
- New script that runs `git checkout`/`pull`/`push` on a shared repo? Take `.git/sync.lock` first. Don't rely on staggered schedules. (§9)
- Touching the cleanup trap in `generate-dashboards.sh`? Don't remove it. Git won't check out a branch that's already checked out in another worktree, so a leftover worktree breaks the next run's handling of `pages` and `snapshots`. (§6)
- Need to publish a generated file from the server? Don't push `main`. Use a temporary worktree on a side branch like `pages` or `snapshots`. (§18)

**Deploys**
- Adding a stack with a database or one-way migrations? Add `deploy.rollback=false` to any service in its compose file, or a failed update will roll back to an old app version on a newer schema. (§16)
- Wondering what the deployer checks for a stack? Run `gitops-deploy.sh --verify <stack>`. It prints the baseline (ports and HTTP endpoints) and runs the checks without deploying. (§16)

**Tools**
- Piping Docker logs to `oracle`? Use `2>&1` (e.g. `docker logs container 2>&1 | oracle`). Docker sends app logs to stderr, which a plain `|` skips. (§13)
- `oracle` hitting Google 503 errors? It switches to `-lite` models by itself. You can also set `GEMINI_MODEL='gemini-flash-lite-latest'` in `.env` to make the lighter model the default. (§13)
- Running `doc-injector.py`? Run it on your dev machine before committing, never on the server. (§14)
- `doc-injector.py` not re-checking a script? It hashes the code without the `@CAPABILITIES` block. Use `--force` to skip the cache. (§14)

**CI and Terraform**
- Setting up a required PR check? Never add `paths` filtering, or docs-only PRs get stuck on `Expected`. (§15.1)
- Adding third-party GitHub Actions? Pin to the full 40-character commit SHA, never a tag or `latest`. (§15.2)
- Testing Compose files in CI? Make sure `.env.example` exists. CI copies it temporarily so required variables resolve. (§15.3)
- Custom S3 endpoint giving `unsupported protocol scheme ""`? The endpoint URL must include `https://`. (§19.2)
- `terraform import` planning to delete a repo's description or homepage? Leaving out an attribute or setting `null` means "wipe it." Pass the existing values in the module call. (§19.3)
- Race during resource replacement (`422 Name already exists`)? Renaming an unmanaged or moved resource runs `create` and `destroy` at the same time. Run `terraform import` for existing cloud assets before applying changes. (§19.3)
- Zsh says `no matches found` on Terraform addresses? Wrap addresses with square brackets (`module.foo.resource["key"]`) in single quotes so the shell doesn't treat them as a pattern.
