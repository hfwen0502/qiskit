#!/usr/bin/env bash
# Re-run bv_n280-all-to-all 5x on each branch to measure timing variability.
BP=/mnt/data/spotter-val/benchpress
cd "$BP"
for br in main26 rebase; do
  V=/mnt/data/spotter-val/wt/$br/.venv/bin/python
  node=0; [ "$br" = rebase ] && node=1
  for i in 1 2 3 4 5; do
    QISKIT_TRANSPILER_SEED=1 numactl --cpunodebind=$node --membind=$node "$V" -m pytest \
      benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k "bv_n280-all-to-all" -q \
      --benchmark-json="/tmp/bv_${br}_$i.json" >/dev/null 2>&1
  done
done
python3 - <<'PY'
import json, statistics
for br in ["main26", "rebase"]:
    walls = []
    meta = None
    for i in range(1, 6):
        b = json.load(open(f"/tmp/bv_{br}_{i}.json"))["benchmarks"][0]
        walls.append(b["stats"]["mean"])
        e = b["extra_info"]
        meta = (e["output_gate_count_2q"], e["output_gate_count_total"], e["output_depth_total"])
    label = "main" if br == "main26" else "PR  "
    print(f"{label} wall(s): " + "  ".join(f"{w:.3f}" for w in walls) +
          f"   mean={statistics.mean(walls):.3f} min={min(walls):.3f} max={max(walls):.3f}"
          f"   (2q/total/depth={meta})")
PY
