#!/bin/bash
# @DESCRIPTION: Resets UFW and applies correct rules (Private Server Mode)
# @FREQUENCY: Run Once (triggered by `setup.sh`)
# @CAPABILITIES:
#   • Instantly resets UFW to a clean state, eliminating legacy rule conflicts.
#   • Enforces a strict deny-all incoming policy, safeguarding the server from unsolicited traffic.
#   • Grants full LAN access, enabling seamless local device connectivity without port juggling.
#   • Trusts the Tailscale VPN interface, allowing secure remote management from anywhere.
#   • Allows Docker subnet traffic, ensuring container communication while keeping external exposure minimal.
# ==============================================================================
# 🛡️ UFW FIREWALL RESTORATION SCRIPT
# Strategy: Block Internet, Trust LAN, Trust VPN, Trust Docker.
# ==============================================================================

echo "--- Resetting Firewall to Defaults ---"
# Reset rules to blank state without confirmation prompt
sudo ufw --force reset

# Default Policies
# Deny incoming (Block the internet)
sudo ufw default deny incoming
# Allow outgoing (Allow updates/pings)
sudo ufw default allow outgoing

echo "--- Applying Rules ---"

# 1. LOCAL LAN TRUST
# Allows your home devices to access ALL ports (SSH, Web 80/443, SMB, etc)
# This replaces the need for specific Port 22 or 80 rules for your PC.
sudo ufw allow from 192.168.0.0/16 comment 'Trust Local LAN'

# 2. TAILSCALE VPN TRUST
# Allow all traffic coming through the VPN tunnel
# This allows you to SSH/Web browse via VPN from anywhere.
sudo ufw allow in on tailscale0 comment 'Trust Tailscale VPN'

# 3. DOCKER NETWORK TRUST
# This 172.16.0.0/12 range covers 172.16.x.x through 172.31.x.x
sudo ufw allow from 172.16.0.0/12 comment 'Trust Docker Containers'

echo "--- Enabling Firewall ---"
# Enable without asking for confirmation
sudo ufw --force enable

echo "--- Status Check ---"
sudo ufw status verbose
