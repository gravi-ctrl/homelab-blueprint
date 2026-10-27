#!/bin/bash
# @DESCRIPTION: Pulls merged GitHub changes (e.g. Renovate PRs) into /opt/stacks, redeploys the affected running stacks, verifies each one (compose --wait, crash-loop soak, automatic port + HTTP checks, optional URL probe) and rolls back to the last known good compose file on failure. Also has read-only --probe and --verify helper modes.
# @FREQUENCY: Every 15 minutes
# @USES_ENV: STACKS_DIR, TELEGRAM_DANTE_BOT_TOKEN, TELEGRAM_CHAT_ID
# @CRON: user
# @CAPABILITIES:
#   • Synchronizes only main branch, blocking feature branches to maintain production stability.
#   • Detects merge conflicts, pauses deployment, and alerts admins via Telegram to prevent broken stacks.
#   • Redeploys only stacks with running containers, preserving intentionally stopped services.
#   • Uses non‑blocking lock to avoid race conditions during concurrent runs.
#   • Pulls images sequentially with retries, ensuring zero‑downtime updates even on transient registry failures.
#   • Tracks failed stacks in a git‑local file so one bad stack never blocks others and is retried automatically.
#   • Verifies every deploy with compose wait, crash‑loop soak, port and HTTP checks, and optional URL probe.
#   • Rolls back failed stack to last known good compose file and alerts via Telegram; deploy.rollback=false opts a stack out.#       Only needed when you want the optional deploy.healthcheck label: tries common health paths and prints a
#       ready-to-paste label. If nothing but "/" answers, skip the label; the automatic checks already cover it.
#
set -euo pipefail

[[ -f "/opt/rabbit-hole/.env" ]] || { echo ".env does not exist at /opt/rabbit-hole" >&2; exit 1; }
source "/opt/rabbit-hole/.env"

cd "${STACKS_DIR}"
PAUSE_FILE="/tmp/gitops_paused"
# Everything below lives inside .git, so it is never committed or synced.
RETRY_FILE="${STACKS_DIR}/.git/gitops_retry"            # stacks to retry next cycle
STATE_DIR="${STACKS_DIR}/.git/gitops_state"
LASTGOOD_DIR="${STATE_DIR}/lastgood"                    # last compose.yml that deployed AND verified OK
REASON_FILE="${STATE_DIR}/last_reason"

# Tunables (override from the environment if a stack needs longer)
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"      # seconds `docker compose up --wait` may take
SOAK_SECONDS="${SOAK_SECONDS:-30}"       # watch for crash-loops this long after it reports ready
PROBE_TIMEOUT="${PROBE_TIMEOUT:-120}"    # seconds a service gets to start listening / answer HTTP / answer its URL after an update

ROLLBACK_OFF_RE='deploy\.rollback["'"'"']?[=:] *["'"'"']?false'

send_telegram() {
    if [ -n "${TELEGRAM_DANTE_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -fsS "https://api.telegram.org/bot${TELEGRAM_DANTE_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=$1" > /dev/null || true
    fi
}

# Image pulls occasionally fail on transient registry/containerd errors; retry before giving up.
pull_with_retry() {
    local attempt
    for attempt in 1 2 3; do
        if docker compose pull -q; then
            return 0
        fi
        echo "⚠️  Pull attempt ${attempt}/3 failed." >&2
        if [ "$attempt" -lt 3 ]; then
            sleep 20
        fi
    done
    return 1
}

# Sum of restart counters for the given container IDs.
restart_total() {
    docker inspect -f '{{.RestartCount}}' "$@" | awk '{s+=$1} END {print s+0}'
}

# First IP of a container on any of its networks (empty for host-network / shared-network containers).
container_ip() {
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" 2>/dev/null | awk '{print $1}'
}

# TCP ports a container declares (image EXPOSE, compose expose: / ports:).
container_tcp_ports() {
    docker inspect -f '{{range $p, $_ := .Config.ExposedPorts}}{{println $p}}{{end}}' "$1" 2>/dev/null \
        | awk -F/ '$2=="tcp" {print $1}'
}

port_open() {   # $1=ip $2=port
    timeout 3 bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$1" "$2" 2>/dev/null
}

any_port_open() {   # $1=ip, remaining args = ports
    local ip="$1" port
    shift
    for port in "$@"; do
        if port_open "$ip" "$port"; then
            return 0
        fi
    done
    return 1
}

# "service port" for every running container of the stack that accepts connections on a declared TCP port right now.
listening_ports() {
    local id svc ip port
    for id in $(docker compose ps -q); do
        svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id" 2>/dev/null) || continue
        ip=$(container_ip "$id")
        [ -n "$ip" ] || continue
        for port in $(container_tcp_ports "$id"); do
            if port_open "$ip" "$port"; then
                echo "$svc $port"
            fi
        done
    done
}

# Every service that was listening before the update must accept connections again (on any port it was
# listening on before, or declares now) within PROBE_TIMEOUT. Prints the reason and returns 1 if not.
wait_listening() {
    local svc id ports ip waited
    for svc in $(printf '%s\n' "$BASE_PORTS" | awk 'NF {print $1}' | sort -u); do
        id=$(docker compose ps -a -q "$svc" | head -n1)
        [ -n "$id" ] || continue          # service no longer exists in the new compose file
        ports=$( { printf '%s\n' "$BASE_PORTS" | awk -v s="$svc" '$1==s {print $2}'; container_tcp_ports "$id"; } | sed '/^$/d' | sort -u)
        waited=0
        # shellcheck disable=SC2086
        until ip=$(container_ip "$id"); [ -n "$ip" ] && any_port_open "$ip" $ports; do
            waited=$((waited + 5))
            if [ "$waited" -ge "$PROBE_TIMEOUT" ]; then
                echo "service ${svc} is not accepting connections (it was before the update)"
                return 1
            fi
            sleep 5
        done
    done
    return 0
}

# Never send HTTP to well-known non-web ports (databases, mail, SSH, DNS, ...).
is_web_port() {
    case "$1" in
        22 | 25 | 53 | 110 | 143 | 389 | 465 | 587 | 636 | 993 | 995 | 1433 | 3306 | 5432 | 5672 | 6379 | 11211 | 27017) return 1 ;;
    esac
    return 0
}

# True if "/" on ip:port answers HTTP or HTTPS with a non-5xx status. $1=ip $2=port
answers_http() {
    local scheme code
    for scheme in http https; do
        code=$(curl -sk -o /dev/null --max-time 5 -w '%{http_code}' "${scheme}://${1}:${2}/" 2> /dev/null < /dev/null || true)
        case "$code" in
            [1-4][0-9][0-9]) return 0 ;;
            000) ;;                 # no HTTP answer on this scheme: try the other one
            *) return 1 ;;          # 5xx (or no status at all): unhealthy
        esac
    done
    return 1
}

any_http() {   # $1=ip, remaining args = ports
    local ip="$1" port
    shift
    for port in "$@"; do
        if is_web_port "$port" && answers_http "$ip" "$port"; then
            return 0
        fi
    done
    return 1
}

# "service port" for every listening port (from BASE_PORTS) that answers HTTP right now.
http_baseline() {
    local svc port id ip
    while read -r svc port; do
        [ -n "$svc" ] || continue
        is_web_port "$port" || continue
        id=$(docker compose ps -q "$svc" < /dev/null | head -n1)
        [ -n "$id" ] || continue
        ip=$(container_ip "$id")
        [ -n "$ip" ] || continue
        if answers_http "$ip" "$port"; then
            echo "$svc $port"
        fi
    done <<< "$BASE_PORTS"
}

# Every service that answered HTTP before the update must answer again (on any port it answered on before, or
# declares now) within PROBE_TIMEOUT. Prints the reason and returns 1 if not.
wait_http() {
    local svc id ports ip waited
    for svc in $(printf '%s\n' "$BASE_HTTP" | awk 'NF {print $1}' | sort -u); do
        id=$(docker compose ps -a -q "$svc" | head -n1)
        [ -n "$id" ] || continue          # service no longer exists in the new compose file
        ports=$( { printf '%s\n' "$BASE_HTTP" | awk -v s="$svc" '$1==s {print $2}'; container_tcp_ports "$id"; } | sed '/^$/d' | sort -u)
        waited=0
        # shellcheck disable=SC2086
        until ip=$(container_ip "$id"); [ -n "$ip" ] && any_http "$ip" $ports; do
            waited=$((waited + 5))
            if [ "$waited" -ge "$PROBE_TIMEOUT" ]; then
                echo "service ${svc} no longer answers HTTP (it did before the update)"
                return 1
            fi
            sleep 5
        done
    done
    return 0
}

# Swap the container/service name in a URL for the container's IP (nothing needs to be published).
resolve_url() {
    local url="$1" host id ip=""
    host=$(printf '%s' "$url" | sed -E 's#^[a-zA-Z]+://([^/:]+).*#\1#')
    id=$(docker compose ps -a -q "$host" 2>/dev/null | head -n1)           # by service name
    if [ -z "$id" ]; then
        id=$(docker inspect -f '{{.Id}}' "$host" 2>/dev/null || true)       # or by container_name
    fi
    if [ -n "$id" ]; then
        ip=$(container_ip "$id")
    fi
    if [ -n "$ip" ]; then
        printf '%s' "${url/\/\/$host/\/\/$ip}"
    else
        printf '%s' "$url"
    fi
}

# deploy.healthcheck URLs of the running stack that answer right now (the others are ignored with a warning).
passing_urls() {
    local id url
    for id in $(docker compose ps -q); do
        docker inspect -f '{{index .Config.Labels "deploy.healthcheck"}}' "$id" 2>/dev/null || true
    done | sort -u | while read -r url; do
        [ -n "$url" ] || continue
        if curl -fsSk -o /dev/null --max-time 5 "$(resolve_url "$url")" 2>/dev/null; then
            echo "$url"
        else
            echo "⚠️  GitOps: deploy.healthcheck ${url} does not answer on the running version; ignoring it." >&2
        fi
    done
}

probe_urls() {
    local url waited
    while read -r url; do
        [ -n "$url" ] || continue
        waited=0
        until curl -fsSk -o /dev/null --max-time 5 "$(resolve_url "$url")" 2>/dev/null; do
            waited=$((waited + 5))
            if [ "$waited" -ge "$PROBE_TIMEOUT" ]; then
                echo "health URL ${url} did not answer within ${PROBE_TIMEOUT}s"
                return 1
            fi
            sleep 5
        done
    done <<< "$BASE_URLS"
    return 0
}

# Run inside a stack dir AFTER `up --wait` succeeded. Prints the reason and returns 1 if unhealthy.
verify_stack() {
    local ids before after bad url waited
    ids=$(docker compose ps -a -q)
    if [ -z "$ids" ]; then
        echo "no containers found after deploy"
        return 1
    fi

    # Crash-loop detection: "running" can still be a container that keeps restarting.
    # shellcheck disable=SC2086
    before=$(restart_total $ids)
    sleep "$SOAK_SECONDS"
    # shellcheck disable=SC2086
    after=$(restart_total $ids)
    if [ "$after" -gt "$before" ]; then
        echo "containers restarted during the ${SOAK_SECONDS}s soak check (crash loop)"
        return 1
    fi

    # shellcheck disable=SC2086
    bad=$(docker inspect -f '{{.Name}} {{.State.Status}} {{.State.ExitCode}} {{with .State.Health}}{{.Status}}{{end}}' $ids \
        | awk '$2=="restarting" || $2=="dead" || $2=="created" || ($2=="exited" && $3!=0) || $4=="unhealthy" {printf "%s(%s) ", $1, $2}')
    if [ -n "$bad" ]; then
        echo "unhealthy container(s): $bad"
        return 1
    fi

    wait_listening || return 1
    wait_http || return 1
    probe_urls || return 1
    return 0
}

# Deploy + verify one stack. Exit codes:
#   0 deployed and verified    1 failed, left as is (retried next cycle)
#   2 failed, rolled back OK   3 failed AND the rollback failed
deploy_stack() (
    dir="$1"
    lastgood="${LASTGOOD_DIR}/${dir}.yml"
    : > "$REASON_FILE"
    cd "$dir" || exit 1

    # First run for this stack: the previous commit's compose file is what was running.
    if [ ! -f "$lastgood" ] && [ "$BEHIND" -gt 0 ] && git cat-file -e "${OLD_HEAD}:${dir}/compose.yml" 2>/dev/null; then
        git show "${OLD_HEAD}:${dir}/compose.yml" > "$lastgood"
    fi

    if ! pull_with_retry; then
        echo "image pull failed" > "$REASON_FILE"
        exit 1
    fi

    # Baselines from the version running NOW: only checks that passed before the update can fail it.
    BASE_PORTS=$(listening_ports) || BASE_PORTS=""
    BASE_HTTP=$(http_baseline) || BASE_HTTP=""
    BASE_URLS=$(passing_urls) || BASE_URLS=""

    if ! docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
        echo "did not become ready within ${WAIT_TIMEOUT}s, or a container exited" > "$REASON_FILE"
    elif verify_stack > "$REASON_FILE"; then
        cp compose.yml "$lastgood"
        exit 0
    fi

    echo "❌ GitOps: $dir failed verification: $(cat "$REASON_FILE")"
    docker compose logs --tail=20 2>&1 || true   # ends up in the cron-guard log

    if grep -Eq "$ROLLBACK_OFF_RE" compose.yml; then
        echo "⏭️  GitOps: $dir has deploy.rollback=false; not rolling back."
        exit 1
    fi
    if [ ! -f "$lastgood" ]; then
        echo "⏭️  GitOps: no last-known-good compose file for $dir; not rolling back."
        exit 1
    fi

    echo "↩️  GitOps: Rolling back $dir to the last known good compose file"
    if docker compose -f "$lastgood" --project-directory "$PWD" up -d --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"; then
        exit 2
    fi
    exit 3
)

# --probe CONTAINER [PORT]: find a working health path for the optional deploy.healthcheck label.
probe_container() {
    local name="${1:-}" only_port="${2:-}" ip ports port scheme code path best_special best_root
    if [ -z "$name" ]; then
        echo "Usage: $(basename "$0") --probe CONTAINER [PORT]" >&2
        return 2
    fi
    if ! docker inspect "$name" > /dev/null 2>&1; then
        echo "No such container: $name (use the container_name, see: docker ps --format '{{.Names}}')" >&2
        return 2
    fi
    ip=$(container_ip "$name")
    if [ -z "$ip" ]; then
        echo "$name has no container IP (host network or shared network namespace), so it can't be probed."
        echo "It still gets the running + crash-loop checks on every deploy."
        return 0
    fi
    ports="${only_port:-$(container_tcp_ports "$name" | tr '\n' ' ')}"
    if [ -z "${ports// /}" ]; then
        echo "$name (ip $ip) declares no TCP ports. Pass one: $(basename "$0") --probe $name PORT"
        echo "or add expose: [\"PORT\"] to its compose file so the automatic port check can see it."
        return 0
    fi
    echo "$name  ip=$ip  ports: $ports"
    for port in $ports; do
        if ! port_open "$ip" "$port"; then
            echo "  port $port: closed (nothing listening)"
            continue
        fi
        scheme=http
        code=$(curl -sk -o /dev/null --max-time 5 -w '%{http_code}' "http://${ip}:${port}/" || true)
        if [ "$code" = "000" ] || [ "$code" = "400" ]; then
            code=$(curl -sk -o /dev/null --max-time 5 -w '%{http_code}' "https://${ip}:${port}/" || true)
            if [ "$code" != "000" ] && [ "$code" != "400" ]; then
                scheme=https
            fi
        fi
        echo "  port $port: open (${scheme})"
        best_special=""
        best_root=""
        for path in / /health /healthz /healthcheck /ping /api/health /api/v1/health /status /api/status /ready /readyz /livez /-/health /admin/; do
            code=$(curl -sk -o /dev/null --max-time 5 -w '%{http_code}' "${scheme}://${ip}:${port}${path}" || true)
            printf '      %-16s %s\n' "$path" "$code"
            case "$code" in
                2* | 3*)
                    if [ "$path" = "/" ]; then
                        best_root="/"
                    elif [ -z "$best_special" ]; then
                        best_special="$path"
                    fi
                    ;;
            esac
        done
        path="${best_special:-$best_root}"
        if [ -n "$path" ]; then
            echo "      -> add to a service in the compose file:"
            echo "           labels:"
            echo "             - \"deploy.healthcheck=${scheme}://${name}:${port}${path}\""
        else
            echo "      -> nothing answered 2xx/3xx (401/403 = needs login, 404 = no such path). Skip the label;"
            echo "         the automatic port and HTTP checks still protect this container."
        fi
    done
}

# --verify STACK: run the post-deploy checks against the stack as it runs right now (deploys nothing).
verify_only() {
    local dir="${1:-}" reason
    if [ -z "$dir" ] || [ ! -f "${STACKS_DIR}/${dir}/compose.yml" ]; then
        echo "Usage: $(basename "$0") --verify STACK   (a folder in ${STACKS_DIR} that has a compose.yml)" >&2
        return 2
    fi
    cd "${STACKS_DIR}/${dir}" || return 2
    if [ -z "$(docker compose ps --services --status=running 2>/dev/null)" ]; then
        echo "⏭️  ${dir} has no running containers; nothing to verify."
        return 0
    fi
    BASE_PORTS=$(listening_ports) || BASE_PORTS=""
    BASE_HTTP=$(http_baseline) || BASE_HTTP=""
    BASE_URLS=$(passing_urls) || BASE_URLS=""
    echo "Baseline for ${dir}:"
    printf '%s\n' "$BASE_PORTS" | awk 'NF {print "  listening     " $1 " port " $2}'
    printf '%s\n' "$BASE_HTTP" | awk 'NF {print "  answers HTTP  " $1 " port " $2}'
    printf '%s\n' "$BASE_URLS" | awk 'NF {print "  url ok        " $0}'
    if [ -z "$BASE_PORTS$BASE_URLS" ]; then
        echo "  (no port or URL could be checked here; only running state and the crash-loop soak apply)"
    fi
    echo "Running the post-deploy checks against what is running right now (nothing is deployed or restarted)..."
    if reason=$(verify_stack); then
        echo "✅ ${dir}: checks pass"
    else
        echo "❌ ${dir}: ${reason}"
        return 1
    fi
}

case "${1:-}" in
    --probe)  shift; rc=0; probe_container "$@" || rc=$?; exit "$rc" ;;
    --verify) shift; rc=0; verify_only "$@" || rc=$?; exit "$rc" ;;
    "") ;;
    *) echo "Usage: $(basename "$0") [--probe CONTAINER [PORT] | --verify STACK]" >&2; exit 2 ;;
esac

# Prevent stepping on git-auto-sync.py or dashboard generation.
# Non-blocking: if busy, skip this run entirely.
exec 200>"${STACKS_DIR}/.git/sync.lock"
flock -n 200 || { echo "⏭️  Stacks repo busy (locked by another process), skipping this cycle."; exit 0; }

mkdir -p "$LASTGOOD_DIR"

# Best effort: pull one image at a time to avoid containerd content-store races.
export COMPOSE_PARALLEL_LIMIT=1

# Verify the server is on 'main' before touching live containers
CURRENT_BRANCH=$(git symbolic-ref --short -q HEAD || echo "detached")

if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "⏭️  Stacks repo is on branch '${CURRENT_BRANCH}' (expected 'main'). Skipping GitOps deploy."
    exit 0
fi

git fetch -q origin main

OLD_HEAD=$(git rev-parse HEAD)
# Count how many remote commits we are missing
BEHIND=$(git rev-list HEAD..origin/main | wc -l)
CHANGED_DIRS=""
NEW_FAILED=""
PREV_FAILED=""
ROLLED_BACK=""
ROLLBACK_FAILED=""

if [ "$BEHIND" -gt 0 ]; then
    echo "🔄 GitOps: Upstream changes detected on main branch."

    # Trigger only if a compose.yml file was modified
    CHANGED_DIRS=$(git diff --name-only HEAD origin/main | grep '/compose\.yml$' | cut -d/ -f1 | sort -u || true)

    # Attempt pull with autostash. Trigger lockout and restore clean state on failure.
    if ! git pull -q origin main --rebase --autostash; then
        git rebase --abort 2>/dev/null || true

        if [ ! -f "$PAUSE_FILE" ]; then
            send_telegram "🚨 GitOps Paused: Merge Conflict Detected
━━━━━━━━━━━━━━━
GitOps has been paused on the server to prevent spam.

Please SSH into the machine and resolve the conflict manually. The script will automatically resume once resolved."
            touch "$PAUSE_FILE"
        fi
        exit 0
    fi

    rm -f "$PAUSE_FILE"
fi

# Add stacks that failed to deploy on an earlier run (git is already up to date for them,
# so they would never show up in the diff again).
if [ -s "$RETRY_FILE" ]; then
    PREV_FAILED=$(sort -u "$RETRY_FILE")
    echo "🔁 GitOps: Retrying previously failed stack(s)."
    CHANGED_DIRS=$(printf '%s\n%s\n' "$CHANGED_DIRS" "$PREV_FAILED" | sed '/^$/d' | sort -u)
fi

# Redeploy active stacks
if [ -n "$CHANGED_DIRS" ]; then
    REDEPLOYED_STACKS=""
    FAILED_DIRS=""

    for dir in $CHANGED_DIRS; do
        if [ -d "$dir" ] && [ -f "$dir/compose.yml" ]; then

            # Check if the stack has active running containers
            if [ -n "$(cd "$dir" && docker compose ps --services --status=running 2>/dev/null)" ]; then
                echo "🚀 GitOps: Redeploying active stack: $dir"
                # A failure here must not abort the loop: the remaining stacks still get deployed.
                rc=0
                deploy_stack "$dir" || rc=$?
                case "$rc" in
                    0) REDEPLOYED_STACKS+="- ${dir}"$'\n' ;;
                    2) ROLLED_BACK+="- ${dir}: $(cat "$REASON_FILE")"$'\n' ;;
                    3) ROLLBACK_FAILED+="- ${dir}: $(cat "$REASON_FILE")"$'\n' ;;
                    *) echo "❌ GitOps: Deploy failed for stack: $dir"
                       FAILED_DIRS+="${dir}"$'\n' ;;
                esac
            else
                echo "⏭️  GitOps: Skipping stopped stack: $dir (Files updated, but container left offline)"
            fi

        fi
    done

    NEW_FAILED=$(printf '%s' "$FAILED_DIRS" | sed '/^$/d' | sort -u)
    if [ -n "$NEW_FAILED" ]; then
        printf '%s\n' "$NEW_FAILED" > "$RETRY_FILE"
    else
        rm -f "$RETRY_FILE"
    fi

    if [ -n "$REDEPLOYED_STACKS" ]; then
        send_telegram "🔄 GitOps Sync: Successful
━━━━━━━━━━━━━━━
The following stacks were successfully updated and restarted:
$REDEPLOYED_STACKS"
    fi

    if [ -n "$ROLLED_BACK" ]; then
        send_telegram "↩️ GitOps: Rolled Back
━━━━━━━━━━━━━━━
The new version failed verification, so the previous compose file is running again:
$ROLLED_BACK
main still contains the bad change. Revert the merged PR on GitHub, and do not run docker compose up in these folders until you have."
    fi

    if [ -n "$ROLLBACK_FAILED" ]; then
        send_telegram "🚨 GitOps: ROLLBACK FAILED
━━━━━━━━━━━━━━━
The new version failed verification and the previous one would not start either:
$ROLLBACK_FAILED
Manual intervention needed. These stacks are not retried automatically."
    fi
fi

# Clear old pause file if the repository is now fully in sync
if [ "$BEHIND" -eq 0 ] && [ -f "$PAUSE_FILE" ]; then
    echo "🧹 GitOps: Repository is in sync. Clearing old pause flag."
    rm -f "$PAUSE_FILE"
fi

FINAL_RC=0
if [ -n "$NEW_FAILED" ]; then
    echo "🚨 GitOps: Deploy failed for: $(printf '%s' "$NEW_FAILED" | tr '\n' ' ')- will retry next cycle."
    # Exit non-zero only when the failure set is new, so cron-guard alerts once instead of every 15 minutes.
    if [ "$NEW_FAILED" != "$PREV_FAILED" ]; then
        FINAL_RC=1
    fi
fi
if [ -n "$ROLLED_BACK" ] || [ -n "$ROLLBACK_FAILED" ]; then
    echo "🚨 GitOps: rollback happened (see above). Revert the merged PR so Git matches what is running."
    FINAL_RC=1
fi

if [ "$FINAL_RC" -eq 0 ] && { [ "$BEHIND" -gt 0 ] || [ -n "$PREV_FAILED" ]; }; then
    echo "✅ GitOps: Sync complete."
fi
exit "$FINAL_RC"
