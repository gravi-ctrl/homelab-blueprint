#!/bin/bash
# @DESCRIPTION: If battery is discharging and under 20%, shutdown the server in 2 minutes
# @FREQUENCY: Every 5 minutes
# @CRON: root
# @CAPABILITIES:
#   • Checks battery level via upower, triggers shutdown if discharging and ≤20%.
#   • Logs critical event with timestamp to /var/log/battery_shutdown.log.
#   • Executes /sbin/shutdown -h +2, scheduling shutdown in two minutes.
#   • Runs as root, scheduled every 5 minutes via cron.
#   • Exits with status 1 after initiating shutdown.
# =============================
# Get battery percentage
BATTERY_LEVEL=$(upower -i /org/freedesktop/UPower/devices/battery_BAT0 | grep percentage | awk '{print $2}' | tr -d '%')
STATUS=$(upower -i /org/freedesktop/UPower/devices/battery_BAT0 | grep state | awk '{print $2}')

# If discharging and under 20%, shut down
if [ "$STATUS" = "discharging" ] && [ "$BATTERY_LEVEL" -le 20 ]; then
    echo "$(date): Battery critical (${BATTERY_LEVEL}%). Shutting down..." >> /var/log/battery_shutdown.log
    /sbin/shutdown -h +2
    exit 1
fi
