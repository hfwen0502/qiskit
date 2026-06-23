# PR #16157 (rebased onto 2.6-dev main) — benchmark evidence & reproduction

Replaces the Level-2 optimization loop's `FixedPoint` convergence check with a
three-signal changed-flag exit. This evidence compares **current `main`** against
**`main` + the PR's 3 commits**, rebased and conflict-resolved onto 2.6-dev
(`origin/main` had parallelized the 1Q-decomposition and CommutationAnalysis
passes since the PR's original base, so the PR was rebased on top — see the
fork branch below).

## Artifacts (this directory)

```
fullsuite_evidence/
  <group>/
    main.json      pytest-benchmark JSON, baseline (origin/main)
    pr.json        pytest-benchmark JSON, PR (main + 3 commits)
    main.stdout    full pytest output, baseline
    pr.stdout      full pytest output, PR
    main.status    main pytest exit code
    pr.status      PR pytest exit code
    environment.txt commits, seed, backend, CPU topology
    analysis.txt   aggregate gate/depth/wall-clock deltas + regression check
  scripts/
    run_group.sh           per-group runner (main@socket0 || PR@socket1)
    analyze.py             per-group analyzer (writes analysis.txt)
    benchpress_patch.diff  benchpress: record 1Q/total gates + 1Q/total depth; seed FlexibleBackend
    000{1,2,3}-*.patch      the 3 PR commits (git am onto origin/main)
    REPRODUCE.md           this file
```

## Environment

- Host: 2-socket Intel Xeon (Sapphire Rapids), 2 NUMA nodes x 40 cores.
- Builds: qiskit built `release + mimalloc` (`QISKIT_BUILD_WITH_MIMALLOC=1 pip install .`).
- `QISKIT_TRANSPILER_SEED=1` for every run.
- Baseline = `origin/main`; PR = baseline + the 3 commits in `scripts/*.patch`.
- Fork branch with the rebased commits: `hfwen0502/qiskit @ test-3signal-rebased-main`.
- Fair A/B: baseline pinned to NUMA socket 0, PR to socket 1, run concurrently
  (`numactl --cpunodebind=N --membind=N`). Verified <2% cross-socket interference.
- benchpress + `benchpress_patch.diff` (records total/1Q gate counts and depths —
  benchpress otherwise records only the 2Q count — and seeds the abstract-group backend).

## To reproduce

```bash
# 1. build baseline + PR (release + mimalloc) in two worktrees, each with a venv
#    + qiskit-ibm-runtime + the benchpress deps.
# 2. apply scripts/benchpress_patch.diff to a benchpress checkout.
# 3. run each group (baseline socket 0, PR socket 1), then analyze:
for g in feynman device_hamiltonians abstract_small abstract_medium abstract_large abstract_hamiltonians; do
    bash scripts/run_group.sh "$g"
    python scripts/analyze.py "$g/main.json" "$g/pr.json" "$g/analysis.txt" "$g"
done
```

Group order matches the PR write-up: device groups (`feynman`, `device_hamiltonians`)
first, then the longer `abstract_*` groups last.
