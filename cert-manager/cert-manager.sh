#!/bin/bash
# @DESCRIPTION: Automates local SSL (mkcert) management: handles CA distribution, multi-service SAN generation, and API-based deployment to Nginx Proxy Manager and Pihole.
# @FREQUENCY: On Demand

# Usage:
#   ./cert-manager.sh init              One-time: install mkcert, create CA
#   ./cert-manager.sh export-ca         Copy CA to shared folder for devices
#   ./cert-manager.sh add <svc> [...]   Add service(s), regen & upload
#   ./cert-manager.sh remove <svc>      Remove a service, regen & upload
#   ./cert-manager.sh regen             Regenerate cert covering all services
#   ./cert-manager.sh upload            Push current cert to NPM via API
#   ./cert-manager.sh list              Show configured services
#   ./cert-manager.sh status            Show cert expiry & SANs
#   ./cert-manager.sh setup-cron        Install monthly auto-renewal cron job

set -euo pipefail

# ════════════════════════════════════════════════════════
#  LOAD ENVIRONMENT
# ════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "[✗] Missing .env file at ${ENV_FILE}" >&2
    echo "" >&2
    echo "    Create it with at minimum:" >&2
    echo "" >&2
    echo '    DOMAIN="homeserver"' >&2
    echo '    SHARE_DIR="/data/assets/syncthing/Shared"' >&2
    echo '    NPM_URL="http://127.0.0.1:81"' >&2
    echo '    NPM_EMAIL="admin@example.com"' >&2
    echo '    NPM_PASS="changeme"' >&2
    echo '    NPM_CERT_NAME="homeserver"' >&2
    echo '    PIHOLE_URL="http://localhost:8081"' >&2
    echo '    PIHOLE_PASS="your-app-password"' >&2
    echo '    SERVER_IP="192.168.1.109"' >&2
    exit 1
fi

# shellcheck source=/dev/null
source "$ENV_FILE"

# Validate required vars
for var in DOMAIN SHARE_DIR NPM_URL NPM_EMAIL NPM_PASS NPM_CERT_NAME; do
    if [[ -z "${!var:-}" ]]; then
        echo "[✗] Required variable '${var}' is not set in .env" >&2
        exit 1
    fi
done

# ════════════════════════════════════════════════════════
#  INTERNALS
# ════════════════════════════════════════════════════════

SERVICES_FILE="${SCRIPT_DIR}/services.list"
CERT_DIR="${SCRIPT_DIR}/certs"
CERT_FILE="homeserver.pem"
KEY_FILE="homeserver-key.pem"

# ── Colours & helpers ──

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }
info() { echo -e "${BLUE}[i]${NC} $*"; }

# ── Services file ──

ensure_services_file() {
    if [[ ! -f "$SERVICES_FILE" ]]; then
        touch "$SERVICES_FILE"
        info "Created empty services file at ${SERVICES_FILE}"
        info "Use '$0 add <service>' to start adding subdomains!"
    fi
}

get_services() {
    grep -v '^#' "$SERVICES_FILE" | grep -v '^[[:space:]]*$' | sort -u
}

# Build the full domain list: "homeserver audiobooks.homeserver bazarr.homeserver …"
build_domains() {
    local domains=("$DOMAIN")
    while IFS= read -r svc; do
        domains+=("${svc}.${DOMAIN}")
    done < <(get_services)
    echo "${domains[@]}"
}

# ── NPM API ──

npm_token=""

npm_auth() {
    [[ -n "$npm_token" ]] && return 0

    local payload
    payload=$(jq -n \
        --arg email "$NPM_EMAIL" \
        --arg pass "$NPM_PASS" \
        '{identity: $email, secret: $pass}')

    local resp code body
    resp=$(curl -s -w "\n%{http_code}" -X POST "${NPM_URL}/api/tokens" \
        -H "Content-Type: application/json" \
        -d "$payload") || {
        err "Could not reach NPM at ${NPM_URL}"; return 1
    }

    code=$(echo "$resp" | tail -1)
    body=$(echo "$resp" | sed '$d')

    if [[ "$code" -ge 400 ]]; then
        err "NPM auth failed (HTTP ${code}): ${body}"; return 1
    fi

    npm_token=$(echo "$body" | jq -r '.token // empty')
    [[ -z "$npm_token" ]] && { err "No token in NPM response"; return 1; }
    log "NPM authentication successful"
}

npm_find_cert() {
    local resp
    resp=$(curl -s "${NPM_URL}/api/nginx/certificates" \
        -H "Authorization: Bearer ${npm_token}") || return 1
    echo "$resp" | jq -r ".[] | select(.nice_name == \"${NPM_CERT_NAME}\") | .id" | head -1
}

npm_create_cert() {
    local payload
    payload=$(jq -n --arg name "$NPM_CERT_NAME" '{nice_name: $name, provider: "other"}')

    curl -s -X POST "${NPM_URL}/api/nginx/certificates" \
        -H "Authorization: Bearer ${npm_token}" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        | jq -r '.id'
}

npm_upload_files() {
    local cert_id="$1" cert_path="$2" key_path="$3"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "${NPM_URL}/api/nginx/certificates/${cert_id}/upload" \
        -H "Authorization: Bearer ${npm_token}" \
        -F "certificate=@${cert_path}" \
        -F "certificate_key=@${key_path}") || { err "Upload curl failed"; return 1; }

    if [[ "$code" == "200" || "$code" == "201" ]]; then
        return 0
    else
        err "Upload returned HTTP ${code}"; return 1
    fi
}

npm_get_proxy_host_id() {
    local domain="$1"
    curl -s "${NPM_URL}/api/nginx/proxy-hosts" \
        -H "Authorization: Bearer ${npm_token}" \
        | jq -r ".[] | select(.domain_names[] == \"${domain}\") | .id" | head -1
}

npm_create_proxy_host() {
    local domain="$1"
    local ip="$2"
    local port="$3"
    local scheme="$4"
    local cert_id="${5:-0}"

    local payload
    payload=$(jq -n \
        --arg domain "$domain" \
        --arg scheme "$scheme" \
        --arg ip "$ip" \
        --argjson port "$port" \
        --argjson cert "$cert_id" \
        '{
            domain_names: [$domain],
            forward_scheme: $scheme,
            forward_host: $ip,
            forward_port: $port,
            certificate_id: (if $cert > 0 then $cert else 0 end),
            ssl_forced: (if $cert > 0 then true else false end),
            hsts_enabled: (if $cert > 0 then true else false end),
            http2_support: (if $cert > 0 then true else false end),
            block_exploits: true,
            allow_websocket_upgrade: true,
            meta: { letsencrypt_agree: false, dns_challenge: false }
        }')

    local resp code body
    resp=$(curl -s -w "\n%{http_code}" -X POST "${NPM_URL}/api/nginx/proxy-hosts" \
        -H "Authorization: Bearer ${npm_token}" \
        -H "Content-Type: application/json" \
        -d "$payload")

    code=$(echo "$resp" | tail -1)
    if [[ "$code" == "200" || "$code" == "201" ]]; then
        log "Created NPM proxy host: ${domain} ➔ ${scheme}://${ip}:${port}"
    else
        body=$(echo "$resp" | sed '$d')
        err "Failed to create proxy host (HTTP ${code}): ${body}"
    fi
}

# Combined: auth → find-or-create → upload
cmd_upload() {
    local cert_path="${CERT_DIR}/${CERT_FILE}"
    local key_path="${CERT_DIR}/${KEY_FILE}"
    [[ -f "$cert_path" ]] || { err "No certificate found. Run: $0 regen"; exit 1; }

    info "Authenticating with NPM..."
    npm_auth || { warn "NPM upload skipped — upload manually via the UI"; return 1; }

    info "Looking for existing certificate '${NPM_CERT_NAME}'..."
    local cert_id
    cert_id=$(npm_find_cert)

    if [[ -z "$cert_id" || "$cert_id" == "null" ]]; then
        info "Creating new certificate entry in NPM..."
        cert_id=$(npm_create_cert)
        [[ -z "$cert_id" || "$cert_id" == "null" ]] && {
            err "Failed to create certificate in NPM"; return 1
        }
        log "Created certificate entry (ID: ${cert_id})"
    else
        log "Found existing certificate (ID: ${cert_id})"
    fi

    info "Uploading certificate files..."
    if npm_upload_files "$cert_id" "$cert_path" "$key_path"; then
        log "Certificate uploaded to NPM successfully"
        info "Any proxy hosts already using '${NPM_CERT_NAME}' will pick it up automatically"
    else
        warn "Auto-upload failed — upload manually:"
        echo "  1. Open NPM → SSL Certificates"
        echo "  2. Edit '${NPM_CERT_NAME}' (or Add → Custom)"
        echo "  3. Upload ${KEY_FILE} and ${CERT_FILE}"
    fi
}

# ── Pihole API ──

pihole_token=""

pihole_auth() {
    [[ -n "$pihole_token" ]] && return 0
    [[ -z "${PIHOLE_PASS:-}" ]] && return 1

    local resp
    resp=$(curl -fsS -X POST "${PIHOLE_URL}/api/auth" \
        -H "Content-Type: application/json" \
        -d "{\"password\":\"${PIHOLE_PASS}\"}" 2>/dev/null) || {
        err "Could not reach Pihole at ${PIHOLE_URL}"; return 1
    }

    pihole_token=$(echo "$resp" | jq -r '.session.sid // empty')
    [[ -z "$pihole_token" ]] && { err "Pihole auth failed"; return 1; }
    log "Pihole authentication successful"
}

pihole_logout() {
    [[ -z "$pihole_token" ]] && return 0
    curl -fsS -X DELETE "${PIHOLE_URL}/api/auth" \
        -H "sid: ${pihole_token}" > /dev/null 2>&1
    pihole_token=""
}

pihole_add_dns() {
    local domain="$1"
    local ip="${SERVER_IP:-192.168.1.109}"

    [[ -z "${PIHOLE_PASS:-}" ]] && { warn "PIHOLE_PASS not set — skipping Pihole DNS record"; return 0; }

    pihole_auth || { warn "Pihole auth failed — add DNS record manually"; return 0; }

    # 1. Fetch current DNS hosts
    local resp
    resp=$(curl -fsS "${PIHOLE_URL}/api/config/dns/hosts" -H "sid: ${pihole_token}") || {
        warn "Pihole API fetch failed — add record manually"; pihole_logout; return 0
    }
    
    local hosts_json
    hosts_json=$(echo "$resp" | jq -c '.config.dns.hosts // []' 2>/dev/null) || {
        warn "Failed to parse Pi-hole DNS records"
        pihole_logout
        return 0
    }
    
    local new_entry="${ip} ${domain}"
    
    # 2. Check if the exact record already exists
    if echo "$hosts_json" | jq -e --arg e "$new_entry" 'index($e) != null' >/dev/null; then
        log "Pihole DNS record already exists: ${domain} → ${ip}"
        pihole_logout
        return 0
    fi

    # 3. Filter out any old entries for this specific domain, then append the new one
    local updated_hosts
    updated_hosts=$(echo "$hosts_json" | jq -c --arg domain "$domain" --arg ip "$ip" '
        map(select(split(" ")[1:] | index($domain) | not)) + [ $ip + " " + $domain ]
    ' 2>/dev/null) || {
        warn "Failed to update Pi-hole DNS records array"
        pihole_logout
        return 0
    }

    # 4. Patch the new config back
    local payload
    payload=$(jq -n --argjson h "$updated_hosts" '{config: {dns: {hosts: $h}}}')

    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X PATCH "${PIHOLE_URL}/api/config" \
        -H "sid: ${pihole_token}" \
        -H "Content-Type: application/json" \
        -d "$payload")

    if [[ "$code" == "200" || "$code" == "201" || "$code" == "204" ]]; then
        log "Pihole DNS record added: ${domain} → ${ip}"
    else
        warn "Failed to add Pihole DNS record (HTTP ${code}) — add manually"
    fi

    pihole_logout
}

pihole_remove_dns() {
    local domain="$1"

    [[ -z "${PIHOLE_PASS:-}" ]] && { warn "PIHOLE_PASS not set — skipping Pihole DNS removal"; return 0; }

    pihole_auth || { warn "Pihole auth failed — remove DNS record manually"; return 0; }

    # 1. Fetch current DNS hosts
    local resp
    resp=$(curl -fsS "${PIHOLE_URL}/api/config/dns/hosts" -H "sid: ${pihole_token}") || {
        warn "Pihole API fetch failed — remove record manually"; pihole_logout; return 0
    }
    
    local hosts_json
    hosts_json=$(echo "$resp" | jq -c '.config.dns.hosts // []' 2>/dev/null) || {
        warn "Failed to parse Pi-hole DNS records"
        pihole_logout
        return 0
    }
    
    # 2. Filter out the domain from the array
    local updated_hosts
    updated_hosts=$(echo "$hosts_json" | jq -c --arg domain "$domain" '
        map(select(split(" ")[1:] | index($domain) | not))
    ' 2>/dev/null) || {
        warn "Failed to update Pi-hole DNS records array"
        pihole_logout
        return 0
    }

    # 3. If nothing changed, it's already gone; return early
    if [[ "$hosts_json" == "$updated_hosts" ]]; then
        log "Pihole DNS record already removed: ${domain}"
        pihole_logout
        return 0
    fi

    # 4. Patch the new config back
    local payload
    payload=$(jq -n --argjson h "$updated_hosts" '{config: {dns: {hosts: $h}}}')

    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X PATCH "${PIHOLE_URL}/api/config" \
        -H "sid: ${pihole_token}" \
        -H "Content-Type: application/json" \
        -d "$payload")

    if [[ "$code" == "200" || "$code" == "201" || "$code" == "204" ]]; then
        log "Pihole DNS record removed: ${domain}"
    else
        warn "Could not remove Pihole DNS record (HTTP ${code}) — remove manually"
    fi

    pihole_logout
}

# ════════════════════════════════════════════════════════
#  COMMANDS
# ════════════════════════════════════════════════════════

cmd_init() {
    info "Installing prerequisites..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq mkcert libnss3-tools openssl jq curl >/dev/null 2>&1
    log "Prerequisites installed (mkcert $(mkcert --version 2>/dev/null || echo 'unknown'))"

    info "Initializing Certificate Authority..."
    mkcert -install
    log "CA created at $(mkcert -CAROOT)"

    ensure_services_file
    mkdir -p "$CERT_DIR"

    echo ""
    log "Init complete!  Next steps:"
    echo "  1.  $0 export-ca          → copy CA to shared folder"
    echo "  2.  Install rootCA on your devices (see guide)"
    echo "  3.  $0 regen              → generate & upload certificates"
}

cmd_export_ca() {
    local caroot
    caroot=$(mkcert -CAROOT)
    [[ -f "${caroot}/rootCA.pem" ]] || { err "No CA found. Run: $0 init"; exit 1; }

    mkdir -p "$SHARE_DIR"
    cp "${caroot}/rootCA.pem" "${SHARE_DIR}/"

    # DER format for Android
    openssl x509 -in "${caroot}/rootCA.pem" -inform PEM \
        -out "${SHARE_DIR}/rootCA.crt" -outform DER

    log "CA exported to ${SHARE_DIR}/:"
    echo "  → rootCA.pem   (Windows MMC, Firefox)"
    echo "  → rootCA.crt   (Android)"
}

cmd_add() {
    [[ $# -eq 0 ]] && { err "Usage: $0 add <service> [ip] [port] [scheme (http/https)]"; exit 1; }
    ensure_services_file

    local svc="$1"
    local ip="${2:-}"
    local port="${3:-}"
    local scheme="${4:-http}"

    svc=$(echo "$svc" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')
    [[ -z "$svc" ]] && { err "Invalid service name"; exit 1; }

    local domain="${svc}.${DOMAIN}"

    # 1. Handle Certificate
    if grep -qx "$svc" "$SERVICES_FILE" 2>/dev/null; then
        warn "'${domain}' is already in the certificate list."
    else
        echo "$svc" >> "$SERVICES_FILE"
        log "Added '${domain}' to certificate list."
        echo ""
        cmd_regen
    fi

    # 2. Handle Pihole DNS
    echo ""
    info "Adding Pihole DNS record..."
    pihole_add_dns "$domain"

    # 3. Handle NPM Routing (if IP and Port are provided)
    if [[ -n "$ip" && -n "$port" ]]; then
        echo ""
        info "Automating NPM Proxy Host creation..."
        if npm_auth; then
            local existing_id
            existing_id=$(npm_get_proxy_host_id "$domain")

            if [[ -n "$existing_id" && "$existing_id" != "null" ]]; then
                warn "Proxy host for '${domain}' already exists (ID: ${existing_id})."
            else
                local cert_id
                cert_id=$(npm_find_cert)
                [[ -z "$cert_id" || "$cert_id" == "null" ]] && cert_id=0
                npm_create_proxy_host "$domain" "$ip" "$port" "$scheme" "$cert_id"
            fi
        else
            warn "Could not authenticate with NPM. Proxy host must be created manually."
        fi
    fi
}

cmd_remove() {
    [[ $# -eq 0 ]] && { err "Usage: $0 remove <service>"; exit 1; }
    ensure_services_file

    local svc="$1"
    local domain="${svc}.${DOMAIN}"

    # 1. Delete from Pihole
    info "Removing Pihole DNS record..."
    pihole_remove_dns "$domain"
    echo ""

    # 2. Delete from NPM
    info "Checking for NPM proxy host..."
    if curl -sf -o /dev/null --connect-timeout 3 "${NPM_URL}/api/" 2>/dev/null; then
        if npm_auth; then
            local host_id
            host_id=$(npm_get_proxy_host_id "$domain")
            if [[ -n "$host_id" && "$host_id" != "null" ]]; then
                local code
                code=$(curl -s -o /dev/null -w "%{http_code}" \
                    -X DELETE "${NPM_URL}/api/nginx/proxy-hosts/${host_id}" \
                    -H "Authorization: Bearer ${npm_token}")
                if [[ "$code" == "200" || "$code" == "201" ]]; then
                    log "Deleted NPM proxy host for '${domain}'"
                else
                    err "Failed to delete proxy host (HTTP $code)"
                fi
            else
                info "No proxy host found in NPM for '${domain}'"
            fi
        fi
    else
        warn "NPM unreachable, skipping proxy host deletion."
    fi

    echo ""

    # 3. Delete from Cert List
    if grep -qx "$svc" "$SERVICES_FILE"; then
        grep -vx "$svc" "$SERVICES_FILE" > "${SERVICES_FILE}.tmp"
        mv "${SERVICES_FILE}.tmp" "$SERVICES_FILE"
        log "Removed '${domain}' from local certificate list"
        echo ""
        cmd_regen
    else
        err "'${svc}' not found in local services list"
    fi
}

cmd_regen() {
    ensure_services_file
    mkdir -p "$CERT_DIR"

    local domains
    domains=$(build_domains)
    local count
    count=$(echo "$domains" | wc -w)

    info "Generating certificate for ${count} domain(s)..."
    echo ""

    # shellcheck disable=SC2086
    mkcert -cert-file "${CERT_DIR}/${CERT_FILE}" \
           -key-file  "${CERT_DIR}/${KEY_FILE}" \
           $domains

    echo ""
    log "Certificate generated"
    echo "  → ${CERT_DIR}/${CERT_FILE}"
    echo "  → ${CERT_DIR}/${KEY_FILE}"

    local expiry
    expiry=$(openssl x509 -in "${CERT_DIR}/${CERT_FILE}" -noout -enddate 2>/dev/null \
             | sed 's/notAfter=//')
    info "Expires: ${expiry}"
    echo ""

    # Try auto-upload to NPM
    if curl -sf -o /dev/null --connect-timeout 3 "${NPM_URL}/api/" 2>/dev/null; then
        cmd_upload
    else
        warn "NPM not reachable at ${NPM_URL}"
        echo "  Upload manually or run:  $0 upload"
    fi

    # Copy to share dir
    if [[ -d "$(dirname "$SHARE_DIR")" ]]; then
        mkdir -p "$SHARE_DIR"
        cp "${CERT_DIR}/${CERT_FILE}" "${CERT_DIR}/${KEY_FILE}" "${SHARE_DIR}/"
        log "Copied cert to ${SHARE_DIR}/"
    fi
}

cmd_list() {
    ensure_services_file
    local count
    count=$(get_services | wc -l)
    echo ""
    info "Configured services (${count}):"
    echo ""
    echo "  ${DOMAIN}  (base)"
    while IFS= read -r svc; do
        echo "  ${svc}.${DOMAIN}"
    done < <(get_services)
    echo ""
}

cmd_status() {
    local cert_path="${CERT_DIR}/${CERT_FILE}"

    if [[ ! -f "$cert_path" ]]; then
        warn "No certificate found at ${cert_path}"
        echo "  Run:  $0 regen"
        return
    fi

    echo ""
    info "Certificate: ${cert_path}"
    echo ""
    echo "  Subject : $(openssl x509 -in "$cert_path" -noout -subject 2>/dev/null | sed 's/subject= *//')"
    echo "  Issuer  : $(openssl x509 -in "$cert_path" -noout -issuer  2>/dev/null | sed 's/issuer= *//')"
    echo "  From    : $(openssl x509 -in "$cert_path" -noout -startdate 2>/dev/null | sed 's/notBefore=//')"
    echo "  Until   : $(openssl x509 -in "$cert_path" -noout -enddate   2>/dev/null | sed 's/notAfter=//')"
    echo ""

    if openssl x509 -in "$cert_path" -noout -checkend 2592000 &>/dev/null; then
        log "Valid for more than 30 days"
    else
        warn "Expires within 30 days!  Run:  $0 regen"
    fi

    echo ""
    info "SANs on this certificate:"
    openssl x509 -in "$cert_path" -noout -ext subjectAltName 2>/dev/null \
        | grep -oP 'DNS:[^, ]+' | sed 's/DNS:/  → /' || echo "  (none found)"

    # Cross-reference with services.list
    echo ""
    ensure_services_file
    local missing=0
    while IFS= read -r svc; do
        if ! openssl x509 -in "$cert_path" -noout -ext subjectAltName 2>/dev/null \
             | grep -q "DNS:${svc}.${DOMAIN}"; then
            warn "Service '${svc}.${DOMAIN}' is in services.list but NOT on the certificate"
            ((missing++))
        fi
    done < <(get_services)

    if ((missing > 0)); then
        echo ""
        warn "${missing} service(s) missing from certificate — run:  $0 regen"
    else
        log "All services in services.list are covered by the certificate"
    fi
    echo ""
}

cmd_setup_cron() {
    local script_path="${SCRIPT_DIR}/cert-manager.sh"
    local cron_comment="# cert-manager: regenerate & upload SSL certs to NPM (1st of every month at 03:00)"
    local cron_line="0 3 1 * * ${script_path} regen >> /var/log/cert-manager.log 2>&1"

    if crontab -l 2>/dev/null | grep -qF "cert-manager.sh regen"; then
        warn "Cron job already exists:"
        crontab -l | grep "cert-manager"
        return
    fi

    (crontab -l 2>/dev/null || true; echo ""; echo "$cron_comment"; echo "$cron_line") | crontab -
    log "Cron job installed: regenerate & upload on the 1st of every month at 03:00"
    echo "  ${cron_line}"
    info "Logs will go to /var/log/cert-manager.log"
}

usage() {
    cat << EOF

  SSL Certificate Manager for Homeserver
  ───────────────────────────────────────

  Usage: $0 <command> [arguments]

  Setup:
    init                    Install mkcert, create CA, prepare environment
    export-ca               Copy rootCA.pem/.crt to shared folder for device import
    setup-cron              Install monthly auto-renewal cron job

  Day-to-day:
    add <svc> [ip port] [s] Add service to cert + Pihole DNS. If IP/Port provided, creates NPM proxy.
                            [s] = scheme (http or https, defaults to http)
    remove <svc>            Delete from Pihole DNS, NPM proxy host, cert list, and regenerate
    regen                   Regenerate the certificate for all services
    upload                  Push current certificate to NPM via API
    list                    Show all configured services
    status                  Show certificate details, expiry & coverage audit

  Examples:
    $0 init
    $0 add jellyfin jellyfin 8096
    $0 add nextcloud nextcloud 443 https
    $0 add just-the-cert-domain
    $0 remove romm
    $0 status

EOF
}

case "${1:-}" in
    init)        cmd_init ;;
    export-ca)   cmd_export_ca ;;
    add)         shift; cmd_add "$@" ;;
    remove)      shift; cmd_remove "$@" ;;
    regen)       cmd_regen ;;
    renew)       cmd_regen ;;
    upload)      cmd_upload ;;
    list)        cmd_list ;;
    status)      cmd_status ;;
    setup-cron)  cmd_setup_cron ;;
    -h|--help|help|"") usage; exit 0 ;;
    *)           usage; exit 1 ;;
esac
