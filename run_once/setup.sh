#!/bin/bash

# ==============================================================================
# 🛡️ SERVER BOOTSTRAP PROTOCOL (BindFS Edition)
# Installs dependencies, configures Docker, Snap, Permissions, and Folders.
# Run this after a fresh OS install.
# ==============================================================================

# Colors for pretty output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== STARTING SERVER BOOTSTRAP ===${NC}"

# 1. SYSTEM UPDATE & DEPENDENCIES
echo -e "${YELLOW}[1/6] Updating System & Installing Tools...${NC}"
sudo apt update && sudo apt upgrade -y
# Install the tools: BindFS (Crucial for the mount), Inotify (Watcher), ACL, etc.
sudo apt install -y curl git unzip acl bindfs inotify-tools python3-pip python3-venv fzf bat micro

# 2. DOCKER INSTALLATION
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}[2/6] Installing Docker...${NC}"
    curl -fsSL https://get.docker.com | sh
    # Add current user to docker group
    sudo usermod -aG docker $USER
    echo "Docker installed. You may need to log out and back in for group changes to take effect."
else
    echo -e "${GREEN}[2/6] Docker already installed.${NC}"
fi

# 3. NEXTCLOUD SNAP
echo -e "${YELLOW}[3/6] Configuring Nextcloud Snap...${NC}"
if ! snap list | grep -q nextcloud; then
    sudo snap install nextcloud
fi
# Connect the interface allowing Snap to see /mnt
sudo snap connect nextcloud:removable-media

# 4. DIRECTORY SKELETON
echo -e "${YELLOW}[4/6] Creating Directory Structure...${NC}"
# Physical Data Paths
sudo mkdir -p /srv/data/assets/torrents
sudo mkdir -p /srv/data/assets/Media/{Movies,Shows,Music,Books,Podcasts}
sudo mkdir -p /srv/data/assets/downloads
# Mount Points
sudo mkdir -p /mnt/assets
sudo mkdir -p /mnt/nextcloud_data

# 5. PERMISSIONS (The Docker-First Approach)
echo -e "${YELLOW}[5/6] Setting Physical Ownership to User 1000...${NC}"

# Target: Assets (Physical Location)
TARGET="/srv/data/assets"

# We give full ownership to User 1000 (gravi-ctrl) so Radarr/Sonarr/n8n are happy.
# Nextcloud will access this via the BindFS mount (Root view).
sudo chown -R 1000:1000 "$TARGET"
sudo chmod -R 775 "$TARGET"

echo "Permissions fixed on $TARGET (Owned by User 1000)"

# 6. PYTHON REQUIREMENTS
echo -e "${YELLOW}[6/6] Installing Python Libs for Automation...${NC}"
# For wifi_robot and other scripts
pip3 install python-dotenv selenium flask --break-system-packages

# ==============================================================================
echo -e "${GREEN}=== BOOTSTRAP COMPLETE ===${NC}"
echo -e "${YELLOW}⚠️  NEXT STEPS (Restoration Phase):${NC}"
echo "1. Restore Docker Configs:"
echo "   git clone git@github.com:gravi-ctrl/server-docker-backup.git /opt/stacks"
echo "2. Restore System Configs (Reference 'run once/system_configs/'):"
echo "   - Copy the BindFS/Mount lines from 'fstab.txt' into '/etc/fstab'."
echo "   - (DO NOT overwrite fstab directly, as Disk UUIDs will change!)."
echo "   - Restore Cronjobs: cat user_crontab.txt | crontab -"
echo "3. Run 'sudo visudo' and add: gravi-ctrl ALL=(root) NOPASSWD: /usr/bin/crontab -l"
echo "4. Reboot."
