#!/bin/bash
# @DESCRIPTION: Check if root (/) or /data exceeds 90% and send an alert
# @FREQUENCY: Every hour
# @CRON: user
# @CAPABILITIES:
#   • Detects root or /data exceeding 90% and triggers immediate alerts.
#   • Enables proactive remediation by exiting with status 1 for downstream automation.
#   • Provides hourly visibility to prevent critical system outages.
#   • Operates without external dependencies for lightweight, maintainable monitoring.

THRESHOLD=90
ROOT_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
DATA_USAGE=$(df /data | awk 'NR==2 {print $5}' | sed 's/%//')

if [ "$ROOT_USAGE" -gt "$THRESHOLD" ] || [ "$DATA_USAGE" -gt "$THRESHOLD" ]; then
    echo "CRITICAL: Disk Space Low! Root: ${ROOT_USAGE}%, Data: ${DATA_USAGE}%"
    exit 1
fi
exit 0
