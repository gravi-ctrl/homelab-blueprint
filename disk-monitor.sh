#!/bin/bash
# @DESCRIPTION: Check if root (/) or /data exceeds 90%
# @FREQUENCY: Every hour
# @CRON: user
# @CAPABILITIES:
#   • Detects root or /data disk usage over 90%, triggering immediate alerts to prevent system outages.
#   • Exits with status 1 on critical usage, enabling cron to log failures and trigger downstream remediation.
#   • Provides hourly visibility, ensuring timely intervention before critical thresholds are breached.
#   • Simplifies monitoring by focusing on key partitions, reducing noise from less critical volumes.
#   • Enables automated alerting without external dependencies, keeping the system lightweight and maintainable.

THRESHOLD=90
ROOT_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
DATA_USAGE=$(df /data | awk 'NR==2 {print $5}' | sed 's/%//')

if [ "$ROOT_USAGE" -gt "$THRESHOLD" ] || [ "$DATA_USAGE" -gt "$THRESHOLD" ]; then
    echo "CRITICAL: Disk Space Low! Root: ${ROOT_USAGE}%, Data: ${DATA_USAGE}%"
    exit 1
fi
exit 0
