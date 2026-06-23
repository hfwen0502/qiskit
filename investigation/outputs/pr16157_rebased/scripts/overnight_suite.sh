#!/usr/bin/env bash
# Repeat the full Benchpress suite N times (default 5), main@socket0 || PR@socket1.
# Saves overnight/iter_<i>/<group>/{main,pr}.json+stdout+status, then auto-aggregates.
ROOT=/mnt/data/spotter-val
BP=$ROOT/benchpress
MAIN=$ROOT/wt/main26
HEAD=$ROOT/wt/rebase
OUT=$ROOT/overnight
PROG=$OUT/progress.log
ITERS=${1:-5}
ORDER="feynman device_hamiltonians abstract_small abstract_medium abstract_large abstract_hamiltonians"
target() {
  case "$1" in
    feynman) echo "benchpress/qiskit_gym/device_transpile/test_feynman.py";;
    device_hamiltonians) echo "benchpress/qiskit_gym/device_transpile/test_hamiltonians.py";;
    abstract_small) echo "benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_small";;
    abstract_medium) echo "benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_medium";;
    abstract_large) echo "benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_large";;
    abstract_hamiltonians) echo "benchpress/qiskit_gym/abstract_transpile/test_hamiltonians.py";;
  esac
}
mkdir -p "$OUT"; rm -f "$OUT/.alldone"
{ echo "Date: $(date -Iseconds)"; echo "Iterations: $ITERS"; echo "Seed: 1";
  echo "Main: $(git -C "$MAIN" rev-parse HEAD)"; echo "PR:   $(git -C "$HEAD" rev-parse HEAD)";
  echo "Benchpress: $(git -C "$BP" rev-parse HEAD) + benchpress_patch.diff";
  lscpu | grep -E "Model name|Socket|NUMA node\(s\)|Core\(s\) per socket"; } > "$OUT/environment.txt"
echo "OVERNIGHT_START $(date -Iseconds) iters=$ITERS" >> "$PROG"
export QISKIT_TRANSPILER_SEED=1
cd "$BP"
for it in $(seq 1 "$ITERS"); do
  echo "ITER_START $it $(date -Iseconds)" >> "$PROG"
  for g in $ORDER; do
    T="$(target "$g")"
    d="$OUT/iter_$it/$g"; mkdir -p "$d"
    ( numactl --cpunodebind=0 --membind=0 $MAIN/.venv/bin/python -m pytest $T -q \
        --benchmark-json="$d/main.json" >"$d/main.stdout" 2>&1; echo $? >"$d/main.status" ) &
    p1=$!
    ( numactl --cpunodebind=1 --membind=1 $HEAD/.venv/bin/python -m pytest $T -q \
        --benchmark-json="$d/pr.json" >"$d/pr.stdout" 2>&1; echo $? >"$d/pr.status" ) &
    p2=$!
    wait $p1; wait $p2
    echo "GROUP_DONE iter=$it $g $(date -Iseconds)" >> "$PROG"
  done
  echo "ITER_DONE $it $(date -Iseconds)" >> "$PROG"
done
python3 "$ROOT/overnight_aggregate.py" "$OUT" > "$OUT/SUMMARY_OVERNIGHT.txt" 2>&1
echo "OVERNIGHT_ALL_DONE $(date -Iseconds)" >> "$PROG"
touch "$OUT/.alldone"
