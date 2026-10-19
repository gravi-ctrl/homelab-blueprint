#!/bin/bash
# @DESCRIPTION: If battery is discharging and under 20%, shutdown the server in 2 minutes
# @FREQUENCY: Every 5 minutes
# @CRON: root
# @CAPABILITIES:
#   • Prevents data loss by triggering a safe shutdown when battery drops below 20%.
#   • Grants a two-minute grace period for processes to finish before power cuts.
#   • Logs timestamped critical events to enable post-mortem analysis of power failures.
#   • Signals error state after shutdown initiation to trigger external monitoring alerts.
#   • Automates power protection via cron, eliminating manual intervention during outages.
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
