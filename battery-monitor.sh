#!/bin/bash
# @DESCRIPTION: If battery is discharging and under 20%, shutdown the server
# @FREQUENCY: Every 5 minutes (root crontab)
# Get battery percentage
BATTERY_LEVEL=$(upower -i /org/freedesktop/UPower/devices/battery_BAT0 | grep percentage | awk '{print $2}' | tr -d '%')
STATUS=$(upower -i /org/freedesktop/UPower/devices/battery_BAT0 | grep state | awk '{print $2}')

# If discharging and under 20%, shut down
if [ "$STATUS" = "discharging" ] && [ "$BATTERY_LEVEL" -le 20 ]; then
    echo "$(date): Battery critical ($BATTERY_LEVEL%). Shutting down..." >> /var/log/battery_shutdown.log
    /sbin/shutdown -h now
fi
