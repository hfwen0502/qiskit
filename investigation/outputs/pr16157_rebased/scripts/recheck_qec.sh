#!/usr/bin/env bash
# Run qec_en_n5-square 8x on MAIN alone to test for pre-existing 1Q non-determinism.
BP=/mnt/data/spotter-val/benchpress
cd "$BP"
V=/mnt/data/spotter-val/wt/main26/.venv/bin/python
for i in $(seq 1 8); do
  QISKIT_TRANSPILER_SEED=1 numactl --cpunodebind=0 --membind=0 "$V" -m pytest \
    benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k "qec_en_n5-square" -q \
    --benchmark-json="/tmp/qec_main_$i.json" >/dev/null 2>&1
done
python3 - <<'PY'
import json
vals = []
for i in range(1, 9):
    e = json.load(open(f"/tmp/qec_main_{i}.json"))["benchmarks"][0]["extra_info"]
    vals.append((e["output_gate_count_2q"], e["output_gate_count_1q"], e["output_gate_count_total"]))
print("main-only qec_en_n5-square over 8 runs  (2Q, 1Q, total):")
for i, v in enumerate(vals, 1):
    print(f"  run {i}: 2Q={v[0]}  1Q={v[1]}  total={v[2]}")
uniq = sorted(set(vals))
print(f"distinct outcomes: {len(uniq)} -> " +
      ("PRE-EXISTING NON-DETERMINISM on main alone" if len(uniq) > 1
       else "deterministic this sample"))
if len(uniq) > 1:
    print("  values seen:", uniq)
PY
