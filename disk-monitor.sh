#!/bin/bash
# @DESCRIPTION: Check if root (/) or /data exceeds 90%
# @FREQUENCY: Every hour
# @CRON: user
# @CAPABILITIES:
#   • Monitors root and /data disk usage, alerting when either exceeds 90%.
#   • Parses df output with awk and sed to extract percentage values.
#   • Exits with status 1 on critical, 0 otherwise, enabling cron error detection.
#   • Scheduled hourly via cron, no signal traps or locking mechanisms present.

THRESHOLD=90
ROOT_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
DATA_USAGE=$(df /data | awk 'NR==2 {print $5}' | sed 's/%//')

if [ "$ROOT_USAGE" -gt "$THRESHOLD" ] || [ "$DATA_USAGE" -gt "$THRESHOLD" ]; then
    echo "CRITICAL: Disk Space Low! Root: ${ROOT_USAGE}%, Data: ${DATA_USAGE}%"
    exit 1
fi
exit 0
