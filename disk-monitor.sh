#!/bin/bash
# @DESCRIPTION: Check if root (/) or /data exceeds 90%
# @FREQUENCY: Every hour
# @CRON: user
# @CAPABILITIES:
#   • Monitors root and /data partitions to prevent critical system outages.
#   • Triggers immediate alerts when disk usage exceeds the 90% threshold.
#   • Exits with status 1 on failure to enable downstream remediation workflows.
#   • Provides hourly visibility for timely intervention before storage becomes full.
#   • Reduces monitoring noise by focusing exclusively on key operational partitions.
#   • Operates without external dependencies, ensuring lightweight and maintainable infrastructure.

THRESHOLD=90
ROOT_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
DATA_USAGE=$(df /data | awk 'NR==2 {print $5}' | sed 's/%//')

if [ "$ROOT_USAGE" -gt "$THRESHOLD" ] || [ "$DATA_USAGE" -gt "$THRESHOLD" ]; then
    echo "CRITICAL: Disk Space Low! Root: ${ROOT_USAGE}%, Data: ${DATA_USAGE}%"
    exit 1
fi
exit 0
