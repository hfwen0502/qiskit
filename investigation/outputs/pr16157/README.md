# PR #16157 evidence

Supporting data for [qiskit#16157](https://github.com/Qiskit/qiskit/pull/16157) —
replacing the Level 2 optimization loop's `FixedPoint` convergence check with a
three-signal loop exit.

## Environment

All measurements on the same Linux server (Intel Xeon SapphireRapids, 2×40 cores
× 2 threads = 160 CPUs across 2 NUMA nodes). Both branches built release with
mimalloc:

```bash
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    pip install -e . --no-build-isolation
```

Seed pinned via `QISKIT_TRANSPILER_SEED=42` (from
[qiskit#16001](https://github.com/Qiskit/qiskit/pull/16001)) — no hardcoded
`seed_transpiler=` in benchpress.

## Benchpress patch

Benchpress stock records only 2Q gate count / depth. To produce the
total-gate-count and overall-depth metrics Matthew asked for, we added
six fields (2Q/1Q/total × count/depth) in a single 10-line patch. See
[`scripts/benchpress_io_patch.diff`](scripts/benchpress_io_patch.diff) — applies
cleanly against `benchpress/qiskit_gym/utils/io.py`.

## Layout

| Dir | Contents |
|-----|----------|
| [`scripts/`](scripts) | `run_feynman.sh` — parallel dual-socket launcher. `compare.py` — main-vs-PR analysis. `benchpress_io_patch.diff` — metrics patch. |
| [`determinism_envvar/`](determinism_envvar) | 3× hwb12 runs on each branch with env-var seed only. Proves `QISKIT_TRANSPILER_SEED=42` alone gives bit-identical output (no hidden randomness). |
| [`numa_sweep/`](numa_sweep) | hwb12 under 4 CPU/memory configs × 3 reps. Shows best-vs-default gap is below 10% threshold → parallel dual-socket (main on node 0, PR on node 1) is the chosen config for evidence runs. |
| [`feynman/`](feynman) | First full-category run: 50 circuits × 2 branches. Correctness pass (bit-identical), −23% wall-clock overall. hwb12 −24%, hwb11 −25%, hwb10 −23%. |

## Headline numbers

### Correctness (feynman, 50 circuits in common)
All six metrics (2Q / 1Q / total count, 2Q / 1Q / total depth) and the
full operation dict are **bit-identical** between main and PR on every circuit.
See [`feynman/analysis.txt`](feynman/analysis.txt).

### Speedup
- **hwb12** (>1M gates entering the loop, Matthew's stress test): 13.98s → 10.62s
  (**−24.0%**).
- **feynman total** (50 circuits): 24.4s → 18.7s (**−23.4%**).
- **No circuit >5% slower on PR** across all 50.

## How to reproduce

1. Build main (baseline) and PR branches per *Environment* above, into
   separate venvs on a single host with two NUMA nodes.
2. Apply [`scripts/benchpress_io_patch.diff`](scripts/benchpress_io_patch.diff)
   to benchpress.
3. Run [`scripts/run_feynman.sh`](scripts/run_feynman.sh) — produces
   `main.json`, `pr.json`, `main.stdout`, `pr.stdout`, `environment.txt`.
4. Run `python scripts/compare.py main.json pr.json` — produces the analysis.
