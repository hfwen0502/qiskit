#!/usr/bin/env bash
# Orchestrates all groups in PR order (device groups first, abstract last),
# running analyze.py after each and appending a one-line result to progress.log
# so an external monitor can report per-group.
ROOT=/mnt/data/spotter-val/fullsuite_evidence
PROG=$ROOT/progress.log
GRP_LIST="feynman device_hamiltonians abstract_small abstract_medium abstract_large abstract_hamiltonians"
echo "RUN_START $(date -Iseconds)" >> "$PROG"
for g in $GRP_LIST; do
  echo "GROUP_START $g $(date -Iseconds)" >> "$PROG"
  bash "$ROOT/scripts/run_group.sh" "$g" > "$ROOT/scripts/$g.runlog" 2>&1
  ms=$(cat "$ROOT/$g/main.status" 2>/dev/null || echo "main missing")
  ps=$(cat "$ROOT/$g/pr.status" 2>/dev/null || echo "pr missing")
  if [ -f "$ROOT/$g/main.json" ] && [ -f "$ROOT/$g/pr.json" ]; then
    python3 "$ROOT/scripts/analyze.py" "$ROOT/$g/main.json" "$ROOT/$g/pr.json" "$ROOT/$g/analysis.txt" "$g" >/dev/null 2>&1
    rc=$?
    gt=$(grep output_gate_count_total "$ROOT/$g/analysis.txt" | tr -s ' ')
    wall=$(grep wall_clock_sum_s "$ROOT/$g/analysis.txt" | tr -s ' ')
    [ $rc -eq 3 ] && st=REGRESSION || st=OK
  else
    st=RUN_FAILED; gt=""; wall=""
  fi
  { [ "$ms" = "main exit 0" ] && [ "$ps" = "pr exit 0" ]; } || st="$st/EXIT($ms;$ps)"
  echo "GROUP_DONE $g $st || $gt || $wall" >> "$PROG"
done
echo "RUN_ALL_DONE $(date -Iseconds)" >> "$PROG"
