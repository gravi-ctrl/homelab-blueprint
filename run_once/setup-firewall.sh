#!/bin/bash

# ==============================================================================
# 🛡️ UFW FIREWALL RESTORATION SCRIPT
# Based on saved configuration.
# ==============================================================================

echo "--- Resetting Firewall to Defaults ---"
# Disable first to prevent lockout
sudo ufw disable
# Reset rules to blank state
echo "y" | sudo ufw reset
# Default Policies
sudo ufw default deny incoming
sudo ufw default allow outgoing

echo "--- Applying Rules ---"

# 1. SSH ACCESS (Restricted to LAN & Docker)
# Prevents public internet SSH access
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp comment 'SSH from LAN'
sudo ufw allow from 172.16.0.0/12 to any port 22 proto tcp comment 'SSH from Docker'

# 2. WEB TRAFFIC (Nginx Proxy Manager / Nextcloud)
# 80/443 TCP is standard web. 443 UDP is for HTTP/3 (QUIC).
sudo ufw allow 80/tcp comment 'HTTP'
sudo ufw allow 443/tcp comment 'HTTPS'
sudo ufw allow 443/udp comment 'HTTPS/3 QUIC'

# 3. LOCAL LAN TRUST
# Allows your home devices to access all ports (SMB, etc)
sudo ufw allow from 192.168.1.0/24 comment 'Trust Local LAN'

# 4. DOCKER NETWORK TRUST
# This 172.16.0.0/12 range covers 172.16.x.x through 172.31.x.x
# This ensures containers can talk to the host (including Unbound at 172.19.0.1)
sudo ufw allow from 172.16.0.0/12 comment 'Trust Docker Containers'

# 5. SPECIFIC UNBOUND DNS (Redundant but Safe)
# Explicitly allowing UDP 5335 from Docker networks
sudo ufw allow from 172.16.0.0/12 to any port 5335 proto udp comment 'Unbound DNS from Docker'

# 6. TAILSCALE VPN
# Allow all traffic coming through the VPN tunnel
sudo ufw allow in on tailscale0 comment 'Trust Tailscale VPN'

echo "--- Enabling Firewall ---"
# Enable without asking for confirmation ('y')
echo "y" | sudo ufw enable

echo "--- Status Check ---"
sudo ufw status verbose
