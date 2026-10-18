#!/bin/bash
# @DESCRIPTION: If battery is discharging and under 20%, shutdown the server in 2 minutes
# @FREQUENCY: Every 5 minutes
# @CRON: root
# @CAPABILITIES:
#   • Prevents data loss by auto‑shutting down when battery drops below 20% during discharging.
#   • Reduces downtime risk by scheduling a safe 2‑minute shutdown, giving processes time to finish.
#   • Provides audit trail with timestamped logs, enabling post‑mortem analysis of critical battery events.
#   • Runs under root via cron, ensuring consistent checks every 5 minutes without manual intervention.
#   • Signals error state after shutdown initiation, allowing monitoring tools to detect and alert on power events.
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
