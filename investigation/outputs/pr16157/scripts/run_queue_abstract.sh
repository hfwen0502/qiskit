#!/usr/bin/env bash
# Re-run abstract_* groups after FlexibleBackend seed fix.
set -u

QUEUE=(abstract_small abstract_medium abstract_large abstract_hamiltonians)
LOG=/mnt/data/pr16157_evidence/queue_abstract.log
echo "=== QUEUE STARTED $(date -Iseconds) ===" > "$LOG"

# Remove old abstract results so reruns land fresh
for group in "${QUEUE[@]}"; do
    rm -rf /mnt/data/pr16157_evidence/$group
done

for group in "${QUEUE[@]}"; do
    echo "[$(date -Iseconds)] >>> starting group: $group" >> "$LOG"
    bash /mnt/data/pr16157_run_group.sh "$group" >> "$LOG" 2>&1
    rc=$?
    echo "[$(date -Iseconds)] <<< group $group exit $rc" >> "$LOG"
    [ -f /mnt/data/pr16157_evidence/$group/main.status ] && echo "   main: $(cat /mnt/data/pr16157_evidence/$group/main.status)" >> "$LOG"
    [ -f /mnt/data/pr16157_evidence/$group/pr.status ] && echo "   pr:   $(cat /mnt/data/pr16157_evidence/$group/pr.status)" >> "$LOG"
done
echo "=== QUEUE DONE $(date -Iseconds) ===" >> "$LOG"
