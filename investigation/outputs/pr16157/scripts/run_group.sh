#!/usr/bin/env bash
# PR #16157 evidence — per-group runner. Runs main on NUMA node 0, PR on
# node 1, in parallel. Saves JSONs + stdouts + environment.txt per group.
#
# Usage: bash pr16157_run_group.sh <group> [<out_root>]
#
# Groups:
#   feynman                - benchpress/qiskit_gym/device_transpile/test_feynman.py
#   device_hamiltonians    - benchpress/qiskit_gym/device_transpile/test_hamiltonians.py
#   abstract_small         - test_qasmbench.py::TestWorkoutAbstractQasmBenchSmall
#   abstract_medium        - test_qasmbench.py::TestWorkoutAbstractQasmBenchMedium
#   abstract_large         - test_qasmbench.py::TestWorkoutAbstractQasmBenchLarge
#   abstract_hamiltonians  - benchpress/qiskit_gym/abstract_transpile/test_hamiltonians.py
#
# Seed: QISKIT_TRANSPILER_SEED=1 (both branches hit the upstream walrus-
# precedence parsing bug so any non-empty value resolves to seed_transpiler=1).
# Using 1 documents the effective seed honestly.
set -euo pipefail

GROUP=${1:?group required}
OUT_ROOT=${2:-/mnt/data/pr16157_evidence}
OUT_DIR="$OUT_ROOT/$GROUP"
mkdir -p "$OUT_DIR"

case "$GROUP" in
    feynman)
        PYTEST_TARGET="benchpress/qiskit_gym/device_transpile/test_feynman.py"
        ;;
    device_hamiltonians)
        PYTEST_TARGET="benchpress/qiskit_gym/device_transpile/test_hamiltonians.py"
        ;;
    abstract_small)
        PYTEST_TARGET="benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_small"
        ;;
    abstract_medium)
        PYTEST_TARGET="benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_medium"
        ;;
    abstract_large)
        PYTEST_TARGET="benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_large"
        ;;
    abstract_hamiltonians)
        PYTEST_TARGET="benchpress/qiskit_gym/abstract_transpile/test_hamiltonians.py"
        ;;
    *)
        echo "Unknown group: $GROUP" >&2
        exit 2
        ;;
esac

export QISKIT_TRANSPILER_SEED=1

{
    echo "Date: $(date -Iseconds)"
    echo "Host: $(hostname)"
    echo "Group: $GROUP"
    echo "Pytest target: $PYTEST_TARGET"
    echo "QISKIT_TRANSPILER_SEED: $QISKIT_TRANSPILER_SEED"
    echo ""
    (cd /mnt/data/qiskit-main && echo "Main build commit: $(git rev-parse HEAD)")
    (cd /mnt/data/qiskit && echo "PR build commit:   $(git rev-parse HEAD)")
    (cd /mnt/data/benchpress && echo "Benchpress commit: $(git rev-parse HEAD)" && echo "Benchpress local changes:" && git status --short)
    echo ""
    echo "CPU topology:"
    lscpu | grep -E "Socket|NUMA|Core|Thread|Model name"
} > "$OUT_DIR/environment.txt"

cd /mnt/data/benchpress

echo "[$GROUP] launching main on node 0, PR on node 1..."
(
    source /mnt/data/myenv-main/bin/activate
    numactl --cpunodebind=0 --membind=0 \
        python -m pytest $PYTEST_TARGET -q \
        --benchmark-json="$OUT_DIR/main.json" \
        > "$OUT_DIR/main.stdout" 2>&1
    echo "main exit $?" > "$OUT_DIR/main.status"
) &
MAIN_PID=$!

(
    source /mnt/data/myenv/bin/activate
    numactl --cpunodebind=1 --membind=1 \
        python -m pytest $PYTEST_TARGET -q \
        --benchmark-json="$OUT_DIR/pr.json" \
        > "$OUT_DIR/pr.stdout" 2>&1
    echo "pr exit $?" > "$OUT_DIR/pr.status"
) &
PR_PID=$!

echo "[$GROUP] main pid=$MAIN_PID  pr pid=$PR_PID"
wait $MAIN_PID
wait $PR_PID

echo "[$GROUP] DONE"
cat "$OUT_DIR/main.status" "$OUT_DIR/pr.status"
tail -2 "$OUT_DIR/main.stdout"
echo "---"
tail -2 "$OUT_DIR/pr.stdout"
