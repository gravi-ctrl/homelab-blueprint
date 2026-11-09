#!/bin/bash
# @DESCRIPTION: After a crash or power loss mid-backup (that got started by `local-opt-backup.sh`, brings the stacks back up from the snapshot the backup left behind.
# @CAPABILITIES:
#   • Detects pending recovery by checking for a snapshot file and exits cleanly if absent
#   • Waits up to five minutes for Docker daemon to become responsive before proceeding
#   • Restores each recorded stack by launching its compose.yml in detached mode
#   • Skips entries without a compose.yml, preventing unnecessary failures
#   • Logs each stack being restored, providing visibility into recovery progress
#   • Captures and reports individual stack launch failures while continuing other recoveries
#   • Deletes the snapshot file after processing to avoid repeated recovery attempts
#   • Returns a non‑zero exit code if any stack failed, enabling downstream error handling

set -euo pipefail
SNAPSHOT="/var/lib/local-opt-backup/running-stacks.list"
[ -f "$SNAPSHOT" ] || exit 0    # clean state: nothing to recover

for _ in $(seq 1 60); do docker info &>/dev/null && break; sleep 5; done
docker info &>/dev/null || { echo "Docker not up after 5 min" >&2; exit 1; }

rc=0
while IFS= read -r dir; do
    [ -f "$dir/compose.yml" ] || continue
    echo "  → $(basename "$dir")"
    docker compose -f "$dir/compose.yml" up -d || { echo "  ⚠️ failed: $dir" >&2; rc=1; }
done < "$SNAPSHOT"
rm -f "$SNAPSHOT"
exit "$rc"
