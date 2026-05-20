#!/bin/bash

# @DESCRIPTION: Automates local SSL (mkcert) management: handles CA distribution, multi-service SAN generation, and API-based deployment to Nginx Proxy Manager.
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
    [[ -f "$SERVICES_FILE" ]] && return
    cat > "$SERVICES_FILE" << 'EOF'
audiobooks
bazarr
dockge
glances
jd
jellyfin
kuma
n8n
nextcloud
npm
paperless
pihole
prowlarr
qbit
radarr
romm
scrutiny
sonarr
syncthing
EOF
    log "Created default ${SERVICES_FILE}"
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
    [[ $# -eq 0 ]] && { err "Usage: $0 add <service> [service2 ...]"; exit 1; }
    ensure_services_file

    local added=0
    for raw in "$@"; do
        local svc
        svc=$(echo "$raw" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')
        [[ -z "$svc" ]] && { warn "Skipping invalid name '${raw}'"; continue; }

        if grep -qx "$svc" "$SERVICES_FILE" 2>/dev/null; then
            warn "'${svc}' already in the list"
        else
            echo "$svc" >> "$SERVICES_FILE"
            log "Added '${svc}.${DOMAIN}'"
            ((added++))
        fi
    done

    if ((added > 0)); then
        echo ""
        cmd_regen
    fi
}

cmd_remove() {
    [[ $# -eq 0 ]] && { err "Usage: $0 remove <service>"; exit 1; }
    ensure_services_file

    local svc="$1"
    if grep -qx "$svc" "$SERVICES_FILE"; then
        grep -vx "$svc" "$SERVICES_FILE" > "${SERVICES_FILE}.tmp"
        mv "${SERVICES_FILE}.tmp" "$SERVICES_FILE"
        log "Removed '${svc}.${DOMAIN}'"
        echo ""
        warn "Remember to also remove the proxy host in NPM if you no longer need it."
        echo ""
        cmd_regen
    else
        err "'${svc}' not found in services list"
        cmd_list
        exit 1
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

# ── Entry point ──

usage() {
    cat << EOF

  SSL Certificate Manager for Homeserver
  ───────────────────────────────────────

  Usage: $0 <command> [arguments]

  Setup:
    init              Install mkcert, create CA, prepare environment
    export-ca         Copy rootCA.pem/.crt to shared folder for device import
    setup-cron        Install monthly auto-renewal cron job

  Day-to-day:
    add <svc> [...]   Add service(s) to the list, regenerate & upload
    remove <svc>      Remove a service, regenerate & upload
    regen             Regenerate the certificate for all services
    upload            Push current certificate to NPM via API
    list              Show all configured services
    status            Show certificate details, expiry & coverage

  Examples:
    $0 init
    $0 add jellyfin grafana
    $0 remove romm
    $0 regen
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
