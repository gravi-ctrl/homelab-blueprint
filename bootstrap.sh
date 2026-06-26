#!/bin/bash
# @DESCRIPTION: Phase 1 Bootstrap: Decrypts & restores a Day-0 archive, fixes SSH permissions, removes cloud-init and re-links blueprint git repositories.
# @FREQUENCY: Run Once (Disaster Recovery)

set -euo pipefail
umask 0022

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================
GIT_HOST="codeberg.org"
GIT_HOST_FALLBACK="github.com"
GIT_USER="gravi-ctrl"
REPO_SCRIPTS="homelab-blueprint"
REPO_CTRL="ctrl-s-master"
REPO_STACKS="server-docker-backup"

DIR_SCRIPTS="$HOME/scripts"
DIR_CTRL="$HOME/ctrl_s_master"
DIR_STACKS="/opt/stacks"

AGE_KEYFILE="/root/.backup-key.txt"
# ==============================================================================

[[ $EUID -eq 0 ]] && { echo "ERROR: Don't run as root." >&2; exit 1; }

echo "======================================================="
echo " 🛡️  SERVER BOOTSTRAP: DEPLOYMENT MODE"
echo "======================================================="
echo "  1) Full Recovery (Restore from age-encrypted backup)"
echo "  2) Fresh Start   (Clone repos, no backup restoration)"
echo "======================================================="
while true; do
    read -r -p "Select an option [1-2]: " choice < /dev/tty
    case $choice in
        1) MODE="RESTORE"; break ;;
        2) MODE="FRESH"; break ;;
        *) echo "⚠️ Invalid option." ;;
    esac
done
echo ""

# ── Init Logging System ───────────────────────────────────────
LOGFILE="/tmp/bootstrap-$(date +%Y%m%d_%H%M%S).log"
CURRENT="preflight"; IN_TASK=false
PASS_COUNT=0; SKIP_COUNT=0
START_TIME=$SECONDS

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
CYAN='\033[0;36m'
NC='\033[0m'

trap '
    $IN_TASK && printf "${RED}✗${NC}\n"
    printf "\n${RED}  ❌ FAILED → %s (line %d)${NC}\n" "$CURRENT" "$LINENO"
    printf "${RED}     See: %s${NC}\n" "$LOGFILE"
    exit 1
' ERR

header()  { printf "\n${CYAN}${BOLD} [%s] %s${NC}\n" "$1" "$2"; }
task()    { CURRENT="$1"; IN_TASK=true; printf "   %-52s " "$1"; echo -e "\n==> $1" >> "$LOGFILE"; }
pass()    { IN_TASK=false; PASS_COUNT=$((PASS_COUNT+1)); printf "${GREEN}✓${NC}"; [ -n "${1:-}" ] && printf " ${DIM}(%s)${NC}" "$1"; printf "\n"; }
skip()    { IN_TASK=false; SKIP_COUNT=$((SKIP_COUNT+1)); printf "${YELLOW}—${NC}"; [ -n "${1:-}" ] && printf " ${DIM}(%s)${NC}" "$1"; printf "\n"; }
quietly() { "$@" >> "$LOGFILE" 2>&1; }

printf "${BOLD} 🛡️  EXECUTING BOOTSTRAP${NC}\n"
printf "    ${DIM}Log → %s${NC}\n" "$LOGFILE"

# Cache sudo to prevent password prompts breaking the quiet logging
if ! sudo -v; then
    echo -e "${RED}❌ Sudo authentication failed.${NC}"; exit 1
fi
while true; do sudo -n true; sleep 60; kill -0 "$$" || exit; done >/dev/null 2>&1 &

# ══════════════════════════════════════════════════════════════
# PHASE 1: RESTORE OR FRESH
# ══════════════════════════════════════════════════════════════
# PHASE 1A: RESTORE
# ═══════════════
if [[ "$MODE" == "RESTORE" ]]; then
    header "PHASE 1A" "Restore Mode"

    task "Verify decryption key and archive"
    sudo [ -f "$AGE_KEYFILE" ] || { echo -e "\n${RED}❌ ERROR: Decryption key not found at $AGE_KEYFILE${NC}"; exit 1; }
    
    BACKUP=$(ls -t "$HOME"/docker-stacks-*.tar.zst.age 2>/dev/null | head -1 || true)
    [[ -z "$BACKUP" ]] && { echo -e "\n${RED}❌ ERROR: No backup archive found in $HOME${NC}"; exit 1; }
    pass "$(basename "$BACKUP")"

    task "Install decryption tools (age, zstd)"
    quietly sudo apt-get update
    quietly sudo NEEDRESTART_SUSPEND=1 apt-get install -y zstd age
    pass

    task "Decrypt and extract system archive"
    sudo age -d -i "$AGE_KEYFILE" "$BACKUP" 2>>"$LOGFILE" | \
    sudo tar --zstd --same-owner --numeric-owner --transform="s,^home/[^/]\+,${HOME#/}," -xf - -C / >>"$LOGFILE" 2>&1
    pass

    task "Fix extracted file ownership"
    if [ -f "/tmp/backup-uid.txt" ]; then
        IFS=: read -r B_UID B_GID < /tmp/backup-uid.txt
        if [[ -n "${B_UID:-}" && -n "${B_GID:-}" ]]; then
            quietly sudo find "$DIR_STACKS" "$DIR_SCRIPTS" "$DIR_CTRL" "$HOME/.ssh" \
                \( -uid "$B_UID" -o -gid "$B_GID" \) ! \( -uid "$(id -u)" -a -gid "$(id -g)" \) \
                -exec chown "$(id -u):$(id -g)" {} + || true
        fi
        quietly sudo rm -f /tmp/backup-uid.txt
        pass "UID/GID updated"
    else
        skip "no mapping file"
    fi

# ══════════════════════════════
# PHASE 1B: FRESH
# ═════════════
elif [[ "$MODE" == "FRESH" ]]; then
    header "PHASE 1B" "Fresh Start"

    task "Validate SSH private keys"
    PRIVATE_KEYS=0
    if [[ -d "$HOME/.ssh" ]]; then
        PRIVATE_KEYS=$(find "$HOME/.ssh" -maxdepth 1 -type f -name "*.pub" 2>/dev/null | while read -r pub; do
            priv="${pub%.pub}"
            [[ -f "$priv" ]] && echo "$priv"
        done | wc -l)
    fi

    if [[ "$PRIVATE_KEYS" -eq 0 ]]; then
        echo -e "\n${RED}❌ ERROR: No SSH private keys found in $HOME/.ssh.${NC}" >&2
        echo -e "   Please place your private key(s) in $HOME/.ssh before running a Fresh Start." >&2
        exit 1
    fi
    pass "$PRIVATE_KEYS key(s) found"
fi

# ══════════════════════════════════════════════════════════════
# PHASE 2: SYSTEM PREP
# ══════════════════════════════════════════════════════════════
header "PHASE 2" "System Prep"

task "Fix SSH permissions"
quietly mkdir -p "$HOME/.ssh"
quietly sudo chown -R "$(id -u):$(id -g)" "$HOME/.ssh"
quietly chmod 700 "$HOME/.ssh"
quietly find "$HOME/.ssh" -type f -exec chmod 600 {} +
quietly find "$HOME/.ssh" -type f -name "*.pub" -exec chmod 644 {} +
pass

task "Remove cloud-init"
quietly sudo NEEDRESTART_SUSPEND=1 apt-get purge -y cloud-init
quietly sudo rm -rf /etc/cloud /etc/ssh/sshd_config.d/50-cloud-init.conf
quietly sudo systemctl restart ssh || true
pass

# ══════════════════════════════════════════════════════════════
# PHASE 3: REPOSITORIES
# ══════════════════════════════════════════════════════════════
header "PHASE 3" "Repositories"

setup_repo() {
    sudo mkdir -p "$1"
    [[ -z "$(ls -A "$1")" ]] && sudo chown -R "$(id -u):$(id -g)" "$1"

    if [ -d "$1/.git" ]; then
        git -C "$1" remote set-url origin "$2" || git -C "$1" remote add origin "$2"
        git -C "$1" fetch origin || return 1
        git -C "$1" rebase origin/main --autostash || true
    else
        git -C "$1" init -b main
        git -C "$1" remote add origin "$2"
        git -C "$1" fetch origin || { rm -rf "$1/.git"; return 1; }
        git -C "$1" checkout -f -B main origin/main
    fi
}

LINK_SUCCESS=true

task "Sync $REPO_SCRIPTS"
if quietly setup_repo "$DIR_SCRIPTS" "git@${GIT_HOST}:${GIT_USER}/${REPO_SCRIPTS}.git" || \
   quietly setup_repo "$DIR_SCRIPTS" "git@${GIT_HOST_FALLBACK}:${GIT_USER}/${REPO_SCRIPTS}.git"; then
    pass
else
    LINK_SUCCESS=false; skip "failed"
fi

task "Sync $REPO_CTRL"
if quietly setup_repo "$DIR_CTRL" "git@${GIT_HOST}:${GIT_USER}/${REPO_CTRL}.git" || \
   quietly setup_repo "$DIR_CTRL" "git@${GIT_HOST_FALLBACK}:${GIT_USER}/${REPO_CTRL}.git"; then
    pass
else
    LINK_SUCCESS=false; skip "failed"
fi

task "Sync $REPO_STACKS"
if quietly setup_repo "$DIR_STACKS" "git@${GIT_HOST}:${GIT_USER}/${REPO_STACKS}.git" || \
   quietly setup_repo "$DIR_STACKS" "git@${GIT_HOST_FALLBACK}:${GIT_USER}/${REPO_STACKS}.git"; then
    pass
else
    LINK_SUCCESS=false; skip "failed"
fi

# ══════════════════════════════════════════════════════════════
# PHASE 4: CLEANUP
# ══════════════════════════════════════════════════════════════
header "PHASE 4" "Cleanup & Summary"

if [[ "$MODE" == "RESTORE" && -n "${BACKUP:-}" ]]; then
    task "Clean up backup archive"
    quietly rm -- "$BACKUP"
    pass
fi

task "Remove bootstrap script from $HOME"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)
if [[ "$SCRIPT_DIR" == "$HOME" ]]; then
    quietly rm -f "${BASH_SOURCE[0]}"
    pass
else
    skip "script not in home dir"
fi

# ══════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════
ELAPSED=$(( SECONDS - START_TIME ))
WARNING_MSG=""
[[ "$LINK_SUCCESS" == false ]] && WARNING_MSG="${RED}\n                         (⚠️ Linking failed! Re-run this script, select \"Fresh Start\", and try again)${NC}"

printf "\n${GREEN}${BOLD} ✅ BOOTSTRAP PHASE COMPLETE${NC} ${DIM}(%dm %ds)${NC}\n" "$((ELAPSED/60))" "$((ELAPSED%60))"
printf "    ${GREEN}%d passed${NC} · ${YELLOW}%d skipped${NC}\n\n" "$PASS_COUNT" "$SKIP_COUNT"
printf "    ${DIM}Full log → %s${NC}\n\n" "$LOGFILE"
printf " ${BOLD}Next steps:${NC}\n"
printf "    1. ${BOLD}Run the installer:${NC} ${DIR_SCRIPTS}/run_once/setup.sh%b\n" "$WARNING_MSG"
printf "    2. ${BOLD}Re-open your SSH session.${NC}\n"