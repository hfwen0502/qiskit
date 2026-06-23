#!/usr/bin/env bash
# PR #16157 (rebased onto 2.6-dev main) evidence — per-group runner.
# Runs main (baseline) on NUMA socket 0 and PR on socket 1, concurrently.
# Saves benchmark JSON + stdout + status + environment per group.
#
# Usage: bash run_group.sh <group>
set -uo pipefail
GROUP=${1:?group required}
ROOT=/mnt/data/spotter-val
OUT="$ROOT/fullsuite_evidence/$GROUP"
MAIN="$ROOT/wt/main26"          # origin/main (2.6-dev) baseline build (+mimalloc)
HEAD="$ROOT/wt/rebase"          # main + 3 PR commits (rebased) build (+mimalloc)
BP="$ROOT/benchpress"           # benchpress + benchpress_patch.diff (total-metrics + seed)
mkdir -p "$OUT"

case "$GROUP" in
  feynman)               T="benchpress/qiskit_gym/device_transpile/test_feynman.py" ;;
  device_hamiltonians)   T="benchpress/qiskit_gym/device_transpile/test_hamiltonians.py" ;;
  abstract_hamiltonians) T="benchpress/qiskit_gym/abstract_transpile/test_hamiltonians.py" ;;
  abstract_small)        T="benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_small" ;;
  abstract_medium)       T="benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_medium" ;;
  abstract_large)        T="benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_large" ;;
  *) echo "unknown group: $GROUP" >&2; exit 2 ;;
esac

export QISKIT_TRANSPILER_SEED=1
{
  echo "Date: $(date -Iseconds)"
  echo "Group: $GROUP"
  echo "Pytest target: $T"
  echo "QISKIT_TRANSPILER_SEED: 1"
  echo "Main build:  $(git -C "$MAIN" rev-parse HEAD)  (socket 0)"
  echo "PR build:    $(git -C "$HEAD" rev-parse HEAD)  (socket 1)"
  echo "Benchpress:  $(git -C "$BP" rev-parse HEAD)  + benchpress_patch.diff"
  echo "Backend (from default.conf): $(grep backend_name "$BP/default.conf")"
  lscpu | grep -E "Model name|Socket|NUMA node\(s\)|Core\(s\) per socket|Thread"
} > "$OUT/environment.txt"

cd "$BP"
echo "[$GROUP] launching main@socket0 + PR@socket1 ..."
( numactl --cpunodebind=0 --membind=0 "$MAIN/.venv/bin/python" -m pytest $T -q \
    --benchmark-json="$OUT/main.json" > "$OUT/main.stdout" 2>&1; echo "main exit $?" > "$OUT/main.status" ) &
P1=$!
( numactl --cpunodebind=1 --membind=1 "$HEAD/.venv/bin/python" -m pytest $T -q \
    --benchmark-json="$OUT/pr.json" > "$OUT/pr.stdout" 2>&1; echo "pr exit $?" > "$OUT/pr.status" ) &
P2=$!
wait $P1; wait $P2
echo "[$GROUP] DONE  ($(cat "$OUT/main.status") | $(cat "$OUT/pr.status"))"
