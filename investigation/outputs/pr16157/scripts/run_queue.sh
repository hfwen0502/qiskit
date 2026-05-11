#!/usr/bin/env bash
# Sequential queue: runs remaining PR #16157 evidence groups, one at a time.
# Each group saves to /mnt/data/pr16157_evidence/<group>/.
# Survives ssh disconnect via nohup. Robust to individual group failures.
set -u

QUEUE=(device_hamiltonians abstract_small abstract_medium abstract_large abstract_hamiltonians)
LOG=/mnt/data/pr16157_evidence/queue.log
echo "=== QUEUE STARTED $(date -Iseconds) ===" > "$LOG"

for group in "${QUEUE[@]}"; do
    echo "[$(date -Iseconds)] >>> starting group: $group" >> "$LOG"
    bash /mnt/data/pr16157_run_group.sh "$group" >> "$LOG" 2>&1
    rc=$?
    echo "[$(date -Iseconds)] <<< group $group exit $rc" >> "$LOG"
    if [ -f /mnt/data/pr16157_evidence/$group/main.status ]; then
        echo "   main: $(cat /mnt/data/pr16157_evidence/$group/main.status)" >> "$LOG"
    fi
    if [ -f /mnt/data/pr16157_evidence/$group/pr.status ]; then
        echo "   pr:   $(cat /mnt/data/pr16157_evidence/$group/pr.status)" >> "$LOG"
    fi
done
echo "=== QUEUE DONE $(date -Iseconds) ===" >> "$LOG"
