#!/bin/bash
# @DESCRIPTION: Check if root (/) or /data exceeds 90%
# @FREQUENCY: Every hour
# @CRON: user

THRESHOLD=90
ROOT_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
DATA_USAGE=$(df /data | awk 'NR==2 {print $5}' | sed 's/%//')

if [ "$ROOT_USAGE" -gt "$THRESHOLD" ] || [ "$DATA_USAGE" -gt "$THRESHOLD" ]; then
    echo "CRITICAL: Disk Space Low! Root: ${ROOT_USAGE}%, Data: ${DATA_USAGE}%"
    exit 1
fi
exit 0
