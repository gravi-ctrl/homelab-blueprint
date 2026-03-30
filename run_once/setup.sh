#!/bin/bash
# @DESCRIPTION: Installs dependencies, configures Docker, permissions, Python, Shell, Runs the firewall script and restores system configs & dotfiles (Run without sudo)
# @FREQUENCY: Run Once
# ==============================================================================
# 🛡️ SERVER BOOTSTRAP PROTOCOL
# Run this after cloning the repo to ~/scripts on a fresh OS.
# ==============================================================================

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

set -e
trap 'echo -e "${RED}❌ Script failed at line $LINENO${NC}"; exit 1' ERR

echo -e "${GREEN}=== STARTING SERVER BOOTSTRAP ===${NC}"

# 0. SUDO VALIDATION
if ! sudo -v; then
    echo -e "${RED}❌ Error: Sudo authentication failed.${NC}"
    exit 1
fi
# Keep sudo alive
while true; do sudo -n true; sleep 60; kill -0 "$$" || exit; done 2>/dev/null &


# 1. SYSTEM UPDATE & DEPENDENCIES
echo -e "${YELLOW}[1/8] Updating System & Installing Tools...${NC}"

# Correct timezone
sudo timedatectl set-timezone Africa/Cairo

# Fix for minimal installs missing add-apt-repository
sudo apt-get update
sudo apt-get install -y software-properties-common

sudo add-apt-repository -y ppa:zhangsongcui3371/fastfetch
sudo add-apt-repository -y ppa:unit193/encryption
sudo apt-get update && sudo apt-get upgrade -y

# Core tools  (rsync added — used later for dotfiles)
sudo apt-get install -y veracrypt btop curl dos2unix age zstd fastfetch unbound moreutils mariadb-client mosh ncdu git zip unzip acl bindfs ufw inotify-tools ntfs-3g samba python3 python3-pip python3-venv fzf bat zsh rsync

# Grant current user read-only access to root crontab (for backups without full sudo)
echo "$USER ALL=(root) NOPASSWD: /usr/bin/crontab -l" | sudo tee "/etc/sudoers.d/backup-cron-$USER" > /dev/null && sudo chmod 0440 "/etc/sudoers.d/backup-cron-$USER"


# 2. DOCKER INSTALLATION & CONFIGURATION
echo -e "${YELLOW}[2/8] Installing & Configuring Docker...${NC}"

if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed."
else
    echo "Docker already installed."
fi

# Docker daemon settings (DNS + log rotation)
sudo mkdir -p /etc/docker
cat <<EOF | sudo tee /etc/docker/daemon.json > /dev/null
{
    "dns": ["192.168.1.109", "1.1.1.1"],
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
EOF
sudo systemctl restart docker
echo -e "${GREEN}✓ Docker daemon configured & restarted${NC}"


# 3. DIRECTORY SKELETON
echo -e "${YELLOW}[3/8] Creating Directory Structure...${NC}"
sudo mkdir -p /data/borg_backup
sudo mkdir -p /data/paperless
sudo mkdir -p /data/assets/torrents
sudo mkdir -p /data/assets/Media/{Movies,Shows,Music,Books,Podcasts}
sudo mkdir -p /data/assets/downloads
sudo mkdir -p /data/assets/romm/{library,resources}
sudo mkdir -p /data/assets/nextcloud_data
sudo mkdir -p /data/assets/syncthing/{Apps,Backup,DCIM,Movies,Music,My_Shit,Shared}

# ── Prepare Data Directories & Permissions ─────────────────────
echo -e "${YELLOW}   Fixing directory permissions...${NC}"
sudo chown -R "$(id -u):$(id -g)" /data
sudo chown -R 33:33 /data/assets/nextcloud_data
sudo setfacl -R -m u:33:rwx /data/assets
sudo setfacl -R -d -m u:33:rwx /data/assets
echo -e "${GREEN}✓ Data directories ready${NC}"


# 4. PYTHON REQUIREMENTS
echo -e "${YELLOW}[4/8] Installing Python Libs...${NC}"
pip3 install python-dotenv cron-descriptor python-telegram-bot selenium flask --break-system-packages


# 5. SHELL ENVIRONMENT (ZSH + P10K)
echo -e "${YELLOW}[5/8] Configuring Zsh Environment...${NC}"

# A. Install Oh-My-Zsh
if [ ! -d "$HOME/.oh-my-zsh" ]; then
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
fi

# B. Install Powerlevel10k
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k" ]; then
    git clone --depth=1 https://github.com/romkatv/powerlevel10k.git "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k"
fi

# C. Install Plugins
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions" ]; then
    git clone https://github.com/zsh-users/zsh-autosuggestions "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions"
fi
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting" ]; then
    git clone https://github.com/zsh-users/zsh-syntax-highlighting.git "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting"
fi

# D. Restore Config Files
DOTFILES_DIR="$HOME/scripts/run_once/dotfiles"

if [ -d "$DOTFILES_DIR" ]; then
    echo "Restoring dotfiles from backup..."

    [ -f "$DOTFILES_DIR/zshrc" ]     && cp "$DOTFILES_DIR/zshrc" "$HOME/.zshrc"
    [ -f "$DOTFILES_DIR/p10k.zsh" ]  && cp "$DOTFILES_DIR/p10k.zsh" "$HOME/.p10k.zsh"
    [ -f "$DOTFILES_DIR/hushlogin" ] && cp "$DOTFILES_DIR/hushlogin" "$HOME/.hushlogin"
    [ -f "$DOTFILES_DIR/nanorc" ]    && sudo cp "$DOTFILES_DIR/nanorc" "/etc/nanorc"

    mkdir -p "$HOME/.config"
    rsync -av "$DOTFILES_DIR/config/" "$HOME/.config/"
else
    echo -e "${RED}Warning: Dotfiles backup not found. Skipping.${NC}"
fi

# E. Set Default Shell
if [ "$SHELL" != "/usr/bin/zsh" ]; then
    echo "Changing default shell to Zsh..."
    sudo chsh -s "$(which zsh)" "$USER"
fi


# 6. UNBOUND DNS RESOLVER
echo -e "${YELLOW}[6/8] Configuring Unbound DNS Resolver...${NC}"

# A. Download latest root hints
sudo mkdir -p /usr/share/dns
wget https://www.internic.net/domain/named.root -qO- | sudo tee /usr/share/dns/root.hints > /dev/null

# B. Write Unbound configuration
sudo mkdir -p /etc/unbound/unbound.conf.d
sudo tee /etc/unbound/unbound.conf.d/pi-hole.conf > /dev/null <<EOF
server:
    # BASICS
    verbosity: 0
    interface: 0.0.0.0  # Listen on all interfaces (Critical)
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

    # PRIVACY (Hide network topology)
    private-address: 192.168.0.0/16
    private-address: 169.254.0.0/16
    private-address: 172.16.0.0/12
    private-address: 10.0.0.0/8

    # ACCESS CONTROL (Who can use this?)
    # 1. Localhost
    access-control: 127.0.0.0/8 allow
    # 2. Home LAN
    access-control: 192.168.0.0/16 allow
    # 3. Docker Containers (Standard Range)
    access-control: 172.16.0.0/12 allow
    # 4. Docker Containers (Alternative Range / VPN)
    access-control: 10.0.0.0/8 allow
EOF

# C. Validate, enable & start
sudo unbound-checkconf
sudo systemctl enable unbound
sudo systemctl restart unbound
echo -e "${GREEN}✓ Unbound DNS configured and running on port 5335${NC}"


# 7. RESTORE SYSTEM CONFIGURATIONS
echo -e "${YELLOW}[7/8] Restoring System Configurations...${NC}"
SYSTEM_CONFIGS_DIR="$HOME/scripts/run_once/system_configs"

if [ -d "$SYSTEM_CONFIGS_DIR" ]; then
    # A. Hosts
    if [ -f "$SYSTEM_CONFIGS_DIR/hosts.txt" ]; then
        sudo cp /etc/hosts "/etc/hosts.backup.$(date +%Y%m%d_%H%M%S)"
        sudo cp "$SYSTEM_CONFIGS_DIR/hosts.txt" /etc/hosts
        sudo chown root:root /etc/hosts
        sudo chmod 644 /etc/hosts
        echo -e "${GREEN}✓ /etc/hosts restored${NC}"
    fi
    # B. User Crontab
    if [ -f "$SYSTEM_CONFIGS_DIR/user_crontab.txt" ]; then
        crontab "$SYSTEM_CONFIGS_DIR/user_crontab.txt"
        echo -e "${GREEN}✓ User crontab restored${NC}"
    fi
    # C. Root Crontab
    if [ -f "$SYSTEM_CONFIGS_DIR/root_crontab.txt" ] && ! grep -q "Root crontab skipped" "$SYSTEM_CONFIGS_DIR/root_crontab.txt"; then
        sudo crontab "$SYSTEM_CONFIGS_DIR/root_crontab.txt"
        echo -e "${GREEN}✓ Root crontab restored${NC}"
    fi
fi


# 8. FIREWALL SETUP
echo -e "${YELLOW}[8/8] Restoring Firewall Rules...${NC}"
FIREWALL_SCRIPT="$HOME/scripts/run_once/setup-firewall.sh"
if [ -f "$FIREWALL_SCRIPT" ]; then
    echo "🔥 Setting up firewall rules..."
    bash "$FIREWALL_SCRIPT"
    echo "✅ Firewall configured."
else
    echo "⚠️  Firewall script not found at: $FIREWALL_SCRIPT"
fi

# ==============================================================================
echo -e "${GREEN}=== BOOTSTRAP COMPLETE ===${NC}"
echo -e "${YELLOW}⚠️  CRITICAL NEXT STEPS:${NC}"
echo ""
echo "1. VERIFY RESTORED ITEMS:"
echo "   ✓ /etc/hosts          - Restored automatically"
echo "   ✓ Crontabs            - Restored automatically"
echo "   ✓ Dotfiles            - Restored automatically"
echo "   ✓ Firewall Rules      - Restored automatically"
echo "   ✓ Docker daemon.json  - Configured automatically"
echo "   ✓ Unbound DNS         - Configured automatically"
echo ""
echo "2. Nextcloud post-restore script:"
echo "   $HOME/scripts/run_once/nextcloud_post-restore_fix.sh"
echo "   (Ignore if no backup file or if nextcloud_data was restored)"
echo "   (Run after `docker compose up -d` on Nextcloud)"
echo ""
echo "3. OPTIONAL - Restore Installed Packages:"
echo "   cat $HOME/scripts/run_once/system_configs/my_installed_apps.txt"
echo "   (Review, then: sudo apt-get install <packages>)"
echo ""
echo "4. REBOOT:"
echo "   sudo reboot"
