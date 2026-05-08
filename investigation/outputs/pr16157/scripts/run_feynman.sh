#!/usr/bin/env bash
# PR #16157 evidence run — feynman suite (includes hwb12).
# Runs main (baseline) and PR branches in parallel on separate NUMA sockets.
# All outputs written to $OUT_DIR for attachment as PR evidence.
#
# Requirements on remote:
#   /mnt/data/qiskit       — PR branch worktree, release+mimalloc build in /mnt/data/myenv
#   /mnt/data/qiskit-main  — main worktree at reference SHA, release+mimalloc build in /mnt/data/myenv-main
#   /mnt/data/benchpress   — benchpress clone with the io.py total-metrics patch
#
# Usage: bash pr16157_run_feynman.sh <out_dir>
set -euo pipefail

OUT_DIR=${1:-/mnt/data/pr16157_evidence/feynman}
mkdir -p "$OUT_DIR"

export QISKIT_TRANSPILER_SEED=42

# Record the exact commits + env being used so it's self-documenting
{
    echo "Date: $(date -Iseconds)"
    echo "Host: $(hostname)"
    echo "QISKIT_TRANSPILER_SEED: $QISKIT_TRANSPILER_SEED"
    echo ""
    echo "Main build:"
    echo "  path: /mnt/data/qiskit-main"
    (cd /mnt/data/qiskit-main && echo "  commit: $(git rev-parse HEAD)")
    echo "PR build:"
    echo "  path: /mnt/data/qiskit"
    (cd /mnt/data/qiskit && echo "  commit: $(git rev-parse HEAD)")
    echo ""
    echo "Benchpress:"
    echo "  path: /mnt/data/benchpress"
    (cd /mnt/data/benchpress && echo "  commit: $(git rev-parse HEAD)" && echo "  local changes:" && git status --short)
    echo ""
    echo "CPU topology:"
    lscpu | grep -E "Socket|NUMA|Core|Thread|Model name"
} > "$OUT_DIR/environment.txt"

cd /mnt/data/benchpress

PYTEST_ARGS="benchpress/qiskit_gym/device_transpile/test_feynman.py -q"

# Launch both branches in parallel on separate sockets
echo "Launching main on socket 0, PR on socket 1..."

(
    source /mnt/data/myenv-main/bin/activate
    numactl --cpunodebind=0 --membind=0 \
        python -m pytest $PYTEST_ARGS \
        --benchmark-json="$OUT_DIR/main.json" \
        > "$OUT_DIR/main.stdout" 2>&1
    echo "MAIN-DONE: exit $?"
) &
MAIN_PID=$!

(
    source /mnt/data/myenv/bin/activate
    numactl --cpunodebind=1 --membind=1 \
        python -m pytest $PYTEST_ARGS \
        --benchmark-json="$OUT_DIR/pr.json" \
        > "$OUT_DIR/pr.stdout" 2>&1
    echo "PR-DONE: exit $?"
) &
PR_PID=$!

echo "main pid=$MAIN_PID, pr pid=$PR_PID"
wait $MAIN_PID
MAIN_EXIT=$?
wait $PR_PID
PR_EXIT=$?

echo "main exit=$MAIN_EXIT, pr exit=$PR_EXIT"
tail -3 "$OUT_DIR/main.stdout"
echo "---"
tail -3 "$OUT_DIR/pr.stdout"
