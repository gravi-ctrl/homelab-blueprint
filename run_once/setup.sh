#!/bin/bash
# @DESCRIPTION: Installs dependencies, configures Docker, Snap, Permissions, Python, and Shell
# @FREQUENCY: Run Once
# ==============================================================================
# 🛡️ SERVER BOOTSTRAP PROTOCOL (Complete Edition)
# Installs dependencies, configures Docker, Snap, Permissions, Python, and Shell.
# Run this after cloning the repo to ~/scripts on a fresh OS.
# ==============================================================================

# Colors for pretty output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== STARTING SERVER BOOTSTRAP ===${NC}"

# 1. SYSTEM UPDATE & DEPENDENCIES
echo -e "${YELLOW}[1/7] Updating System & Installing Tools...${NC}"
# Adding any needed custom repos
sudo add-apt-repository -y ppa:zhangsongcui3371/fastfetch
# Updating and upgrading
sudo apt update && sudo apt upgrade -y
# Core tools + File System tools (ACL/Inotify) + Shell tools (Zsh/FZF)
sudo apt install -y curl dos2unix fastfetch fail2ban unbound moreutils mariadb-client htop mosh ncdu git unzip acl bindfs veracrypt ufw inotify-tools ntfs-3g samba python3-pip python3-venv fzf bat micro zsh

# 2. DOCKER INSTALLATION
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}[2/7] Installing Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    # Add current user to docker group
    sudo usermod -aG docker $USER
    echo "Docker installed. Group changes apply after logout/reboot."
else
    echo -e "${GREEN}[2/7] Docker already installed.${NC}"
fi

# 3. DIRECTORY SKELETON
echo -e "${YELLOW}[4/7] Creating Directory Structure...${NC}"
# Physical Data Paths
sudo mkdir -p /srv/data/assets/torrents
sudo mkdir -p /srv/data/assets/Media/{Movies,Shows,Music,Books,Podcasts}
sudo mkdir -p /srv/data/assets/downloads
sudo mkdir -p /srv/data/assets/romm/{library,resources}
sudo mkdir -p /srv/data/assets/nextcloud_data

# 4. PERMISSIONS (The Hybrid Setup)
echo -e "${YELLOW}[5/7] Applying Permission Fixes...${NC}"

# Target: Assets (Physical Location)
TARGET="/srv/data/assets"
TARGET2="/srv/data/assets/nextcloud_data"

# A. Set Physical Ownership
sudo chown -R $(id -u):$(id -g) "$TARGET"
sudo chmod -R 775 "$TARGET"
sudo chown -R 33:33 "$TARGET2"

# B. Apply ACLs (The Side Door for User 33/Docker)
# Grant rwx (Read/Write/Execute) to user 33 recursively
sudo setfacl -R -m u:33:rwx "$TARGET"
# Set the "Default" ACL so any NEW files created there also get these permissions
sudo setfacl -R -d -m u:33:rwx "$TARGET"

echo "Permissions fixed on $TARGET and $TARGET2"

# 5. PYTHON REQUIREMENTS
echo -e "${YELLOW}[6/7] Installing Python Libs for Automation...${NC}"
# For wifi_robot and other scripts
# Note: Using --break-system-packages is standard for user scripts on Ubuntu 24.04+
pip3 install python-dotenv cron-descriptor python-telegram-bot selenium flask --break-system-packages

# 6. SHELL ENVIRONMENT (ZSH + P10K)
echo -e "${YELLOW}[7/7] Configuring Zsh Environment...${NC}"

# A. Install Oh-My-Zsh (Unattended)
if [ ! -d "$HOME/.oh-my-zsh" ]; then
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
fi

# B. Install Powerlevel10k Theme
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k" ]; then
    git clone --depth=1 https://github.com/romkatv/powerlevel10k.git ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k
fi

# C. Install Plugins (Autosuggestions & Syntax Highlighting)
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions" ]; then
    git clone https://github.com/zsh-users/zsh-autosuggestions ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions
fi
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting" ]; then
    git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting
fi

# D. Restore Config Files (From your Git Backup)
# We assume the repo is already cloned to ~/scripts
DOTFILES_DIR="$HOME/scripts/run_once/dotfiles"

if [ -d "$DOTFILES_DIR" ]; then
    echo "Restoring .zshrc, .p10k.zsh, .hushlogin, .nanorc and fastfetch's config from backup..."
    cp "$DOTFILES_DIR/zshrc" "$HOME/.zshrc"
    cp "$DOTFILES_DIR/p10k.zsh" "$HOME/.p10k.zsh"
    cp "$DOTFILES_DIR/nanorc" "$HOME/.nanorc"
    cp "$DOTFILES_DIR/hushlogin" "$HOME/.hushlogin"
    mkdir -p "$HOME/.config"
    cp -r "$DOTFILES_DIR/config/." "$HOME/.config"

else
    echo -e "${RED}Warning: Dotfiles backup not found in scripts folder. Skipping restore.${NC}"
fi

# E. Set Default Shell
if [ "$SHELL" != "/usr/bin/zsh" ]; then
    echo "Changing default shell to Zsh..."
    sudo chsh -s $(which zsh) $USER
fi

# ==============================================================================
echo -e "${GREEN}=== BOOTSTRAP COMPLETE ===${NC}"
echo -e "${YELLOW}⚠️  NEXT STEPS (Restoration Phase):${NC}"
echo "1. Restore Docker Configs:"
echo "   git clone git@github.com:gravi-ctrl/server-docker-backup.git /opt/stacks"
echo "2. Restore System Configs (Reference 'scripts/run_once/system_configs/'):"
echo "   - Copy the BindFS lines from 'fstab.txt' into '/etc/fstab' (Keep new UUIDs!)."
echo "   - Restore Cronjobs: cat user_crontab.txt | crontab -"
echo "3. Run 'sudo visudo' and add: gravi-ctrl ALL=(root) NOPASSWD: /usr/bin/crontab -l"
echo "4. Restore Firewall: ./scripts/setup-firewall.sh"
echo "5. Reboot."
