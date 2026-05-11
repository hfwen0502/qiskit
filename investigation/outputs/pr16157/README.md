# PR #16157 evidence

Supporting data for [qiskit#16157](https://github.com/Qiskit/qiskit/pull/16157) —
replacing the Level 2 optimization loop's `FixedPoint` convergence check with a
three-signal loop exit.

## Headline

Across **1,023 circuits** in the qiskit_gym transpile suite of
[benchpress](https://github.com/Qiskit/benchpress):

- **1,021 / 1,023 bit-identical** between main and PR.
- 2 circuits show small 1Q-only rotational drift caused by a
  [pre-existing upstream Qiskit non-determinism](scripts/bug2_repro.py)
  (unseeded RNG in a 1Q rotation tie-break). Not caused by this PR;
  2Q gate count identical on both.
- **No circuit regressed** (0 circuits slower on PR by >5%).
- Every group faster overall (−9.5% to −22.7%).

## Per-group results

| Group | Tests | Bit-identical | Total time main (s) | Total time PR (s) | Speedup |
|---|---:|---:|---:|---:|---:|
| [feynman](feynman) | 50 (3 skipped) | **50/50** | 24.8 | 19.1 | **−22.7%** |
| [device_hamiltonians](device_hamiltonians) | 81 (19 skipped) | 80/81 ⁽¹⁾ | 34.1 | 28.1 | **−17.5%** |
| [abstract_small](abstract_small) | 168 | **168/168** | 1.1 | 1.0 | **−9.5%** |
| [abstract_medium](abstract_medium) | 92 | **92/92** | 98.6 | 79.8 | **−19.1%** |
| [abstract_large](abstract_large) | 232 | **232/232** | 603.1 | 497.7 | **−17.5%** |
| [abstract_hamiltonians](abstract_hamiltonians) | 400 | 399/400 ⁽²⁾ | 212.5 | 190.3 | **−10.4%** |
| **Total** | **1,023** | **1,021 (99.8%)** | 974.2 | 816.0 | **−16.2%** |

⁽¹⁾ `ham_mu_z_prime_enc_gray_dvalues_4-4-4-4` — upstream Bug 2 (see below).
⁽²⁾ `ham_ham_JW24` on all-to-all 24Q — upstream Bug 2.

Stress-test circuit Matthew called out (`hwb12`, >1M gates entering the loop):
**14.40s → 10.85s (−24.7%)**.

## Environment

All measurements on a single Linux server (Intel Xeon SapphireRapids, 2×40
cores × 2 threads = 160 CPUs across 2 NUMA nodes). Both Qiskit builds made
from source with release + mimalloc:

```bash
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    pip install -e . --no-build-isolation
```

Seed: `QISKIT_TRANSPILER_SEED=1` in the shell for every pytest invocation.
Both branches run in parallel per group on separate NUMA nodes:

- main on node 0: `numactl --cpunodebind=0 --membind=0 pytest ...`
- PR on node 1:   `numactl --cpunodebind=1 --membind=1 pytest ...`

NUMA sweep in [`numa_sweep/`](numa_sweep) validated that single-socket pinning
matches default-full-machine within 1.7% on hwb12; worst-vs-best 9.9%. See
[`numa_sweep/analysis.txt`](numa_sweep/analysis.txt).

## Benchpress patch

[`scripts/benchpress_patch.diff`](scripts/benchpress_patch.diff) — two changes:

1. `benchpress/qiskit_gym/utils/io.py`: add total/1Q gate count + total/1Q
   depth alongside the existing 2Q-only metrics. Matthew asked for this.
2. `benchpress/utilities/backends/flexible_backend.py`: add `seed=12345` to
   the `GenericBackendV2.__init__` call. Without this, each Python process
   generates different random error rates, so main vs PR see different
   backend targets on abstract tests. With the fix, the abstract runs match
   bit-for-bit across branches.

## Upstream Qiskit issues found (not caused by PR #16157)

Two independent bugs surfaced during this investigation:

### Bug 1: walrus precedence in `QISKIT_TRANSPILER_SEED` parsing

File: `qiskit/transpiler/preset_passmanagers/generate_preset_pass_manager.py`

```python
# Current (buggy):
if seed := os.getenv("QISKIT_TRANSPILER_SEED", None) is not None:
    seed_transpiler = int(seed)

# Fix:
if (seed := os.getenv("QISKIT_TRANSPILER_SEED", None)) is not None:
    seed_transpiler = int(seed)
```

Operator precedence makes `seed` always be `True`/`False`, so `int(seed)`
is always 0 or 1 regardless of what the env var was set to. Same pattern
exists in `qiskit/compiler/transpiler.py:269`.

Reproducer: [`scripts/bug1_walrus_precedence.py`](scripts/bug1_walrus_precedence.py) (pure Python, no Qiskit dependency).

For this PR's evidence we set `QISKIT_TRANSPILER_SEED=1` to document the
effective seed honestly.

### Bug 2: unseeded randomness in L2 pipeline

Even with `seed_transpiler=42` (or any explicit seed) passed directly,
two circuits out of 1,023 produce non-deterministic output across fresh
Python subprocesses: `ham_mu_z_prime_enc_gray_dvalues_4-4-4-4` and
`ham_ham_JW24` on all-to-all 24Q. 2Q gate counts identical across runs;
a small number of 1Q gates shift between equivalent rotational
decompositions. Suggests a tie-break in a 1Q rotation pass that consumes
process-dependent state (e.g., `ahash` iteration order, unseeded
`RandomState`).

Reproducer: [`scripts/bug2_repro.py`](scripts/bug2_repro.py).

## Directory layout

| Path | Contents |
|---|---|
| `feynman/`, `device_hamiltonians/`, `abstract_{small,medium,large,hamiltonians}/` | Per-group {`main.json`, `pr.json`, `main.stdout`, `pr.stdout`, `environment.txt`, `analysis.txt`} |
| `determinism_envvar/` | Early 3×3 determinism check using env-var seed only |
| `numa_sweep/` | hwb12 under 4 CPU/memory configs × 3 reps |
| `scripts/run_group.sh` | Per-group parallel-sockets launcher |
| `scripts/run_queue.sh`, `run_queue_abstract.sh` | Sequential queue scripts used for this run |
| `scripts/compare.py` | main.json vs pr.json analysis |
| `scripts/benchpress_patch.diff` | Benchpress changes required |
| `scripts/bug1_walrus_precedence.py` | Reproducer for upstream Bug 1 |
| `scripts/bug2_repro.py` | Reproducer for upstream Bug 2 |
| `logs/queue.log`, `queue_abstract.log` | Queue execution logs |

## Reproducing from this directory

1. Build two Qiskit installs (main + PR branch) per *Environment* above,
   each in its own venv.
2. Apply [`scripts/benchpress_patch.diff`](scripts/benchpress_patch.diff) to
   a benchpress clone.
3. Run each group via
   [`scripts/run_group.sh <group>`](scripts/run_group.sh),
   or all five via
   [`scripts/run_queue.sh`](scripts/run_queue.sh).
4. Compare with
   [`scripts/compare.py <group>/main.json <group>/pr.json`](scripts/compare.py).
