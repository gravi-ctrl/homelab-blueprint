#!/bin/bash
# @DESCRIPTION: Installs dependencies, configures Docker, Permissions, Python, and Shell
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

echo -e "${GREEN}=== STARTING SERVER BOOTSTRAP ===${NC}"

# 1. SYSTEM UPDATE & DEPENDENCIES
echo -e "${YELLOW}[1/6] Updating System & Installing Tools...${NC}"
sudo add-apt-repository -y ppa:zhangsongcui3371/fastfetch
sudo apt-get update && sudo apt-get upgrade -y

# Core tools
sudo apt-get install -y btop curl dos2unix zstd fastfetch unbound moreutils mariadb-client mosh ncdu git zip unzip acl bindfs veracrypt ufw inotify-tools ntfs-3g samba python3-pip python3-venv fzf bat zsh

# 2. DOCKER INSTALLATION
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}[2/6] Installing Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker installed."
else
    echo -e "${GREEN}[2/6] Docker already installed.${NC}"
fi

# 3. DIRECTORY SKELETON
echo -e "${YELLOW}[3/6] Creating Directory Structure...${NC}"
sudo mkdir -p /srv/data/assets/torrents
sudo mkdir -p /srv/data/assets/Media/{Movies,Shows,Music,Books,Podcasts}
sudo mkdir -p /srv/data/assets/downloads
sudo mkdir -p /srv/data/assets/romm/{library,resources}
sudo mkdir -p /srv/data/assets/nextcloud_data

# 4. PYTHON REQUIREMENTS
echo -e "${YELLOW}[4/6] Installing Python Libs...${NC}"
# Using --break-system-packages because this is a dedicated server environment
pip3 install python-dotenv cron-descriptor python-telegram-bot selenium flask --break-system-packages

# 5. SHELL ENVIRONMENT (ZSH + P10K)
echo -e "${YELLOW}[5/6] Configuring Zsh Environment...${NC}"

# A. Install Oh-My-Zsh
if [ ! -d "$HOME/.oh-my-zsh" ]; then
    sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended
fi

# B. Install Powerlevel10k
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k" ]; then
    git clone --depth=1 https://github.com/romkatv/powerlevel10k.git ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/themes/powerlevel10k
fi

# C. Install Plugins
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions" ]; then
    git clone https://github.com/zsh-users/zsh-autosuggestions ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-autosuggestions
fi
if [ ! -d "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting" ]; then
    git clone https://github.com/zsh-users/zsh-syntax-highlighting.git ${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/zsh-syntax-highlighting
fi

# D. Restore Config Files
DOTFILES_DIR="$HOME/scripts/run_once/dotfiles"

if [ -d "$DOTFILES_DIR" ]; then
    echo "Restoring dotfiles from backup..."
    cp "$DOTFILES_DIR/zshrc" "$HOME/.zshrc"
    cp "$DOTFILES_DIR/p10k.zsh" "$HOME/.p10k.zsh"
    cp "$DOTFILES_DIR/nanorc" "$HOME/.nanorc"
    cp "$DOTFILES_DIR/hushlogin" "$HOME/.hushlogin"

    mkdir -p "$HOME/.config"
    rsync -av "$DOTFILES_DIR/config/" "$HOME/.config/"
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
echo -e "${YELLOW}⚠️  CRITICAL NEXT STEPS:${NC}"
echo "1. Restore FSTAB (Mount Drives):"
echo "   - Edit /etc/fstab and add your drive UUIDs."
echo "   - RUN: 'sudo mount -a' to verify."
echo "2. FIX PERMISSIONS (Only AFTER mounting drives):"
echo "   - Run: sudo chown -R $(id -u):$(id -g) /srv/data/assets"
echo "   - Run: sudo chown -R 33:33 /srv/data/assets/nextcloud_data"
echo "   - Run: sudo setfacl -R -m u:33:rwx /srv/data/assets"
echo "   - Run: sudo setfacl -R -d -m u:33:rwx /srv/data/assets"
echo "3. Restore Cronjobs:"
echo "   - cat $HOME/scripts/run_once/system_configs/user_crontab.txt | crontab -"
echo "4. Reboot."
