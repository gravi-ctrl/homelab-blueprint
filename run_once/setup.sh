#!/bin/bash
# @DESCRIPTION: Full server bootstrap for disaster recovery — restores packages, Docker, directories, dotfiles, DNS, firewall and crontabs on a fresh OS.
# @FREQUENCY: Run Once (Disaster Recovery)
# @USES_ENV: SERVER_IP, DATA_DIR
# ==============================================================================
# 🛡️ SERVER BOOTSTRAP PROTOCOL
# Run this after cloning the repo to ~/scripts on a fresh OS.
# ==============================================================================

# ── Init ──────────────────────────────────────────────────────
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

set -e
trap '
    $IN_TASK && printf "${RED}✗${NC}\n"
    printf "\n${RED}  ❌ FAILED → %s (line %d)${NC}\n" "$CURRENT" "$LINENO"
    printf "${RED}     See: %s${NC}\n" "$LOGFILE"
    exit 1
' ERR

# ── Helpers ───────────────────────────────────────────────────
header()  { printf "\n${CYAN}${BOLD} [%s] %s${NC}\n" "$1" "$2"; }
task()    { CURRENT="$1"; IN_TASK=true; printf "   %-52s " "$1"; echo -e "\n==> $1" >> "$LOGFILE"; }
pass()    { IN_TASK=false; PASS_COUNT=$((PASS_COUNT+1)); printf "${GREEN}✓${NC}"; [ -n "${1:-}" ] && printf " ${DIM}(%s)${NC}" "$1"; printf "\n"; }
skip()    { IN_TASK=false; SKIP_COUNT=$((SKIP_COUNT+1)); printf "${YELLOW}—${NC}"; [ -n "${1:-}" ] && printf " ${DIM}(%s)${NC}" "$1"; printf "\n"; }
quietly() { "$@" >> "$LOGFILE" 2>&1; }

# ══════════════════════════════════════════════════════════════
# PREFLIGHT
# ══════════════════════════════════════════════════════════════
if [[ $EUID -eq 0 ]]; then
    echo -e "${RED}ERROR: Do not run this script as root.${NC}"; exit 1
fi

[[ -f "$HOME/scripts/.env" ]] || { echo -e "${RED}ERROR: .env does not exist at $HOME/scripts.${NC}" >&2; exit 1; }
source "$HOME/scripts/.env"

printf "\n${BOLD} 🛡️  SERVER BOOTSTRAP${NC}\n"
printf "    ${DIM}Log → %s${NC}\n" "$LOGFILE"

if ! sudo -v; then
    echo -e "${RED}❌ Sudo authentication failed.${NC}"; exit 1
fi
while true; do sudo -n true; sleep 60; kill -0 "$$" || exit; done 2>/dev/null &


# ══════════════════════════════════════════════════════════════
# [1/10] SYSTEM UPDATE & DEPENDENCIES
# ══════════════════════════════════════════════════════════════
header "1/10" "System Update & Dependencies"

task "Set timezone → Africa/Cairo"
quietly sudo timedatectl set-timezone Africa/Cairo
pass

task "Install base packages (curl, git, rsync, ufw)"
quietly sudo apt-get update
quietly sudo apt-get install -y software-properties-common curl git rsync ufw
pass

task "Restore custom PPAs"
REPOS_FILE="$HOME/scripts/run_once/system_configs/my_repos.txt"
if [ -f "$REPOS_FILE" ] && [ -s "$REPOS_FILE" ]; then
    while IFS= read -r ppa; do
        quietly sudo add-apt-repository -y --no-update "$ppa"
    done < "$REPOS_FILE"
    pass
else
    skip "not found"
fi

task "Full system upgrade"
quietly sudo apt-get update
quietly sudo apt-get upgrade -y
pass

task "Configure sudoers for backup cron"
echo "$USER ALL=(root) NOPASSWD: /usr/bin/crontab -l" | sudo tee "/etc/sudoers.d/backup-cron-$USER" > /dev/null
sudo chmod 0440 "/etc/sudoers.d/backup-cron-$USER"
pass


# ══════════════════════════════════════════════════════════════
# [2/10] DOCKER INSTALLATION & CONFIGURATION
# ══════════════════════════════════════════════════════════════
header "2/10" "Docker Installation & Configuration"

task "Install Docker engine"
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com 2>>"$LOGFILE" | sh >>"$LOGFILE" 2>&1
    quietly sudo usermod -aG docker "$USER"
    pass "installed"
else
    pass "already installed"
fi

task "Write Docker daemon.json"
sudo mkdir -p /etc/docker
# Docker DNS: primary = Pi-hole/Unbound (update if server IP changes), fallback = Cloudflare
cat <<EOF | sudo tee /etc/docker/daemon.json > /dev/null
{
    "dns": ["${SERVER_IP}", "1.1.1.1"],
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
EOF
pass

task "Restart Docker daemon"
quietly sudo systemctl restart docker
pass

task "Create the shared network"
quietly sudo docker network create --subnet=172.20.0.0/16 proxy || true
pass

# ══════════════════════════════════════════════════════════════
# [3/10] RESTORE INSTALLED PACKAGES
# ══════════════════════════════════════════════════════════════
header "3/10" "Restore Installed Packages"

task "Install packages from backup list"
PACKAGES_FILE="$HOME/scripts/run_once/system_configs/my_installed_apps.txt"
if [ -f "$PACKAGES_FILE" ]; then
    quietly xargs -a "$PACKAGES_FILE" sudo apt-get install -y --ignore-missing
    pass
else
    skip "list not found"
fi


# ══════════════════════════════════════════════════════════════
# [4/10] DIRECTORY STRUCTURE & PERMISSIONS
# ══════════════════════════════════════════════════════════════
header "4/10" "Directory Structure & Permissions"

task "Create /data directory tree"
sudo mkdir -p /data/nextcloud_data
sudo mkdir -p /data/paperless/{data,media}
sudo mkdir -p "${DATA_DIR}"/{torrents,downloads/watch}
sudo mkdir -p "${DATA_DIR}"/romm/{library,resources}
sudo mkdir -p "${DATA_DIR}"/Media/{Movies,Shows,Music,Books,Podcasts}
sudo mkdir -p "${DATA_DIR}"/syncthing/{Apps,Backup,DCIM/paperless-scan,Movies,Music,My_Shit,Shared}
pass

task "Set ownership & ACLs"
sudo chown -R "$(id -u):$(id -g)" /data
sudo chown -R 33:33 /data/nextcloud_data
quietly sudo setfacl -R -m u:33:rwx "${DATA_DIR}"
quietly sudo setfacl -R -d -m u:33:rwx "${DATA_DIR}"
pass

task "Fix .local ownership"
sudo chown -R "$(id -u):$(id -g)" "$HOME/.local"
pass

task "Symlink user scripts → /opt/scripts"
if [ ! -L /opt/scripts ]; then
    sudo ln -s "$HOME/scripts" /opt/scripts
    pass "created"
else
    pass "already linked"
fi

# ══════════════════════════════════════════════════════════════
# [5/10] PYTHON LIBRARIES
# ══════════════════════════════════════════════════════════════
header "5/10" "Python Libraries"

task "Install pip packages from backup list"
PIP_PACKAGES_FILE="$HOME/scripts/run_once/system_configs/my_pip_packages.txt"
if [ -f "$PIP_PACKAGES_FILE" ] && [ -s "$PIP_PACKAGES_FILE" ]; then
    quietly xargs -a "$PIP_PACKAGES_FILE" pip3 install --break-system-packages
    pass
else
    skip "list not found"
fi

# ══════════════════════════════════════════════════════════════
# [6/10] SHELL ENVIRONMENT (ZSH + P10K)
# ══════════════════════════════════════════════════════════════
header "6/10" "Shell Environment (Zsh + P10k)"

task "Install Oh My Zsh"
if [ ! -d "$HOME/.oh-my-zsh" ]; then
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended >>"$LOGFILE" 2>&1
    pass "installed"
else
    pass "already installed"
fi

task "Install Powerlevel10k theme"
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k" ]; then
    quietly git clone --depth=1 https://github.com/romkatv/powerlevel10k.git \
        "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k"
    pass "cloned"
else
    pass "already installed"
fi

task "Install zsh-autosuggestions"
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions" ]; then
    quietly git clone https://github.com/zsh-users/zsh-autosuggestions \
        "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions"
    pass "cloned"
else
    pass "already installed"
fi

task "Install zsh-syntax-highlighting"
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting" ]; then
    quietly git clone https://github.com/zsh-users/zsh-syntax-highlighting.git \
        "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting"
    pass "cloned"
else
    pass "already installed"
fi

task "Restore dotfiles"
DOTFILES_DIR="$HOME/scripts/run_once/dotfiles"
if [ -d "$DOTFILES_DIR" ]; then
    [ -f "$DOTFILES_DIR/zshrc" ]     && cp "$DOTFILES_DIR/zshrc" "$HOME/.zshrc"
    [ -f "$DOTFILES_DIR/p10k.zsh" ]  && cp "$DOTFILES_DIR/p10k.zsh" "$HOME/.p10k.zsh"
    [ -f "$DOTFILES_DIR/hushlogin" ] && cp "$DOTFILES_DIR/hushlogin" "$HOME/.hushlogin"
    [ -f "$DOTFILES_DIR/nanorc" ]    && sudo cp "$DOTFILES_DIR/nanorc" "/etc/nanorc"
    mkdir -p "$HOME/.config"
    quietly rsync -av "$DOTFILES_DIR/config/" "$HOME/.config/"
    pass
else
    skip "dotfiles dir not found"
fi

task "Set default shell → zsh"
if [ "$SHELL" != "/usr/bin/zsh" ]; then
    sudo chsh -s "$(which zsh)" "$USER"
    pass "changed"
else
    pass "already zsh"
fi


# ══════════════════════════════════════════════════════════════
# [7/10] UNBOUND DNS RESOLVER
# ══════════════════════════════════════════════════════════════
header "7/10" "Unbound DNS Resolver"

task "Download root hints"
sudo mkdir -p /usr/share/dns
wget https://www.internic.net/domain/named.root -qO- | sudo tee /usr/share/dns/root.hints > /dev/null
pass

task "Write Unbound config"
sudo mkdir -p /etc/unbound/unbound.conf.d
sudo tee /etc/unbound/unbound.conf.d/pi-hole.conf > /dev/null <<EOF
server:
    # BASICS
    verbosity: 0
    interface: 0.0.0.0
    port: 5335
    do-ip4: yes
    do-udp: yes
    do-tcp: yes
    do-ip6: no
    prefer-ip6: no

    # ROOT SERVERS
    root-hints: "/usr/share/dns/root.hints"

    # SECURITY
    harden-glue: yes
    harden-dnssec-stripped: yes
    use-caps-for-id: no

    # PRIVACY
    private-address: 192.168.0.0/16
    private-address: 169.254.0.0/16
    private-address: 172.16.0.0/12
    private-address: 10.0.0.0/8

    # ACCESS CONTROL
    access-control: 127.0.0.0/8 allow
    access-control: 192.168.0.0/16 allow
    access-control: 172.16.0.0/12 allow
    access-control: 10.0.0.0/8 allow
EOF
pass

task "Validate & start Unbound"
quietly sudo unbound-checkconf
quietly sudo systemctl enable unbound
quietly sudo systemctl restart unbound
pass "port 5335"

task "Disable systemd-resolved stub listener"
quietly sudo sed -i 's/#DNSStubListener=yes/DNSStubListener=no/' /etc/systemd/resolved.conf
quietly sudo systemctl restart systemd-resolved
pass

# ══════════════════════════════════════════════════════════════
# [8/10] SYSTEM CONFIGURATIONS
# ══════════════════════════════════════════════════════════════
header "8/10" "System Configurations"

SYSTEM_CONFIGS_DIR="$HOME/scripts/run_once/system_configs"

task "Restore /etc/hosts"
if [ -f "$SYSTEM_CONFIGS_DIR/hosts.txt" ]; then
    if ! cmp -s "$SYSTEM_CONFIGS_DIR/hosts.txt" /etc/hosts; then
        sudo cp /etc/hosts "/etc/hosts.backup.$(date +%Y%m%d_%H%M%S)"
        sudo cp "$SYSTEM_CONFIGS_DIR/hosts.txt" /etc/hosts
        sudo chown root:root /etc/hosts
        sudo chmod 644 /etc/hosts
        pass "updated"
    else
        pass "already current"
    fi
else
    skip "not found"
fi

task "Restore user crontab"
if [ -f "$SYSTEM_CONFIGS_DIR/user_crontab.txt" ]; then
    crontab "$SYSTEM_CONFIGS_DIR/user_crontab.txt"
    pass
else
    skip "not found"
fi

task "Restore root crontab"
if [ -f "$SYSTEM_CONFIGS_DIR/root_crontab.txt" ] && ! grep -q "Root crontab skipped" "$SYSTEM_CONFIGS_DIR/root_crontab.txt"; then
    sudo crontab "$SYSTEM_CONFIGS_DIR/root_crontab.txt"
    pass
else
    skip "not found or empty"
fi

task "Trust restored mkcert CA system-wide"
if [ -d "$HOME/.local/share/mkcert" ] && [ -f "$HOME/.local/share/mkcert/rootCA.pem" ]; then
    quietly mkcert -install
    pass
else
    skip "no CA found"
fi


# ══════════════════════════════════════════════════════════════
# [9/10] FIREWALL
# ══════════════════════════════════════════════════════════════
header "9/10" "Firewall Rules"

task "Run firewall setup script"
FIREWALL_SCRIPT="$HOME/scripts/run_once/configure-firewall.sh"
if [ -f "$FIREWALL_SCRIPT" ]; then
    quietly bash "$FIREWALL_SCRIPT"
    pass
else
    skip "script not found"
fi

# ══════════════════════════════════════════════════════════════
# [10/10] POST-BOOTSTRAP BACKGROUND WATCHER
# ══════════════════════════════════════════════════════════════
header "10/10" "Background Tasks"
task "Create Docker watcher daemon"

WATCHER_SCRIPT="$HOME/scripts/run_once/container-watcher.sh"

if [ -f "$WATCHER_SCRIPT" ]; then
    chmod +x "$WATCHER_SCRIPT"

    cat << EOF | sudo tee /etc/systemd/system/container-watcher.service > /dev/null
[Unit]
Description=Post-Bootstrap Docker Watcher
After=docker.service

[Service]
Type=simple
ExecStart=/opt/scripts/run_once/container-watcher.sh
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    quietly sudo systemctl enable --now container-watcher.service
    pass "ghost watcher installed & started"
else
    skip "watcher script not found"
fi


# ══════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════
ELAPSED=$(( SECONDS - START_TIME ))
printf "\n${GREEN}${BOLD} ✅ BOOTSTRAP COMPLETE${NC} ${DIM}(%dm %ds)${NC}\n" "$((ELAPSED/60))" "$((ELAPSED%60))"
printf "    ${GREEN}%d passed${NC} · ${YELLOW}%d skipped${NC}\n\n" "$PASS_COUNT" "$SKIP_COUNT"
printf "    ${DIM}Full log → %s${NC}\n\n" "$LOGFILE"
printf " ${BOLD}Next steps:${NC}\n"
printf "    1. ${BOLD}Reboot:${NC} sudo reboot\n"
printf "    2. ${BOLD}Start Containers:${NC} Go to /opt/stacks and start your containers whenever you're ready.\n"
printf "       ${DIM}(A background service will detect when they start and auto-configure them!)${NC}\n"
printf "    3. ${BOLD}Borgmatic:${NC} Mount external HDD, then run:\n"
printf "       borg key import /mnt/external_hdd/borg-repo ~/borg-key-backup.txt\n"
printf "       borgmatic check\n"