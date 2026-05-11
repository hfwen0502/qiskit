# PR #16157 evidence

Supporting data for [qiskit#16157](https://github.com/Qiskit/qiskit/pull/16157) —
replacing the Level 2 optimization loop's `FixedPoint` convergence check
with a three-signal loop exit.

## TL;DR

Across **1,023 circuits** in the qiskit_gym transpile suite of
[benchpress](https://github.com/Qiskit/benchpress) at `optimization_level=2`:

| Metric (sum across 1,023 circuits) | Main | PR | Δ |
|---|---:|---:|---:|
| 2Q gate count | 32,835,273 | 32,835,273 | **0 (+0.00%)** |
| 1Q gate count | 88,889,343 | 88,889,349 | +6 (+0.00001%) |
| Total gate count | 121,724,616 | 121,724,622 | +6 (+0.000005%) |
| 2Q depth | 18,810,148 | 18,810,148 | **0 (+0.00%)** |
| 1Q depth | 35,512,930 | 35,512,932 | +2 |
| Total depth | 54,071,888 | 54,071,890 | +2 |
| Wall-clock (sum of means) | 974.2 s | 816.0 s | **−16.2%** |

- **1,021 / 1,023 circuits produce bit-identical output** between main and PR
  (all six metrics + full op dict per circuit).
- The 2 non-matches (`ham_mu_z_prime_enc_gray_dvalues_4-4-4-4`, `ham_ham_JW24`
  on all-to-all 24Q) are driven by a pre-existing upstream Qiskit
  non-determinism that affects both branches equally — **not caused by
  this PR**. 2Q count is unchanged on both. Details in
  [Upstream bugs](#upstream-bugs) and reproducer at
  [`scripts/bug2_repro.py`](scripts/bug2_repro.py).
- **Zero circuits slower on PR by >5%.** Every group faster in aggregate;
  speedup range −9.5% to −22.7% per group.
- Matthew's stress test (`hwb12`, >1M gates entering the loop):
  **14.40 s → 10.85 s (−24.7%)**.

---

## What changes

- **Level 2** optimization loop: replace the `FixedPoint("depth", "size")`
  convergence check with three orthogonal signals. Exit the loop iff **none**
  fire; otherwise re-iterate.
- **Level 3** loop: unchanged. Keeps its existing `MinimumPoint` logic.
- **Code**: 6 files, +69/-11 lines.
  - Rust (3 files): return booleans from `CommutativeCancellation`,
    `RemoveIdentityEquiv`, `Optimize1qGatesDecomposition` indicating whether
    they created new optimization opportunities.
  - Python (2 files): set `property_set` flags from the Rust return values.
  - Pass manager (1 file): new `_optimization_check_changed_flag()` that
    replaces `_optimization_check_fixed_point()` *only at Level 2*.

---

## Why the change is valid (correctness argument)

The L2 optimization loop runs:

```
Optimize1qGatesDecomposition
CommutativeInverseCancellation
RemoveIdentityEquivalent
ConsolidateBlocks (idle wire contraction)
CommutativeCancellation
GatesInBasis → BasisTranslator → Optimize1qGatesDecomposition (if out-of-basis)
```

Inside this loop, there are **exactly three mechanisms** by which a pass
can create new optimization opportunities for a subsequent iteration:

1. **Multi-qubit gate removal** — `RemoveIdentityEquivalent` or
   `CommutativeCancellation` deletes a 2Q gate. The 1Q runs on either side
   of that gate merge into a single longer 1Q run that
   `Optimize1qGatesDecomposition` can then recombine.
   → **Signal `_opt_pass_changed`** is set when either pass removes a
     multi-qubit gate.

2. **Rotation consolidation** — `CommutativeCancellation` merges adjacent
   rotations on the same axis. This *shortens* a 1Q run, giving
   `Optimize1qGatesDecomposition` new material to work with (the pass can
   now reach beyond what it had previously consolidated).
   → **Signal `_opt_1q_consolidated`** is set when `CommutativeCancellation`
     consolidates rotations. Fires at most once per iteration since
     consolidation is idempotent.

3. **Out-of-basis gate production** — `CommutativeCancellation` can emit a
   rotation outside the target basis. `GatesInBasis` detects this and runs
   `BasisTranslator`, which emits an unoptimized decomposition that needs
   another `Optimize1qGatesDecomposition` pass to clean up.
   → **Signal `all_gates_in_basis == False`**, set by the existing
     `GatesInBasis` analysis pass that already runs in the loop.

**Why these three are sufficient**: any pass that runs inside the loop
either mutates the DAG in ways covered by one of the signals, or its
output is already locally optimal and a second invocation would produce
identical results. Specifically, `Optimize1qGatesDecomposition` mutates
unconditionally (it always rebuilds the canonical form) but is stateless
across runs — if no pass changed the 1Q runs around it, its second
invocation yields the same DAG. That's why tracking "did Optimize1q
change anything" is not a reliable signal and not used.

**Why `FixedPoint("depth", "size")` is overkill at L2**: FixedPoint runs
one confirmation iteration after depth + size stop changing. With our
signals, the same information is available from the passes themselves
without the extra iteration. Eliminating that extra iteration is the
source of the speedup.

---

## Per-group results

| Group | Tests (skipped) | Bit-identical | Total main | Total PR | Speedup |
|---|---:|---:|---:|---:|---:|
| [feynman](feynman) | 50 (3) | **50/50** | 24.8 s | 19.1 s | **−22.7%** |
| [device_hamiltonians](device_hamiltonians) | 81 (19) | 80/81 ⁽¹⁾ | 34.1 s | 28.1 s | **−17.5%** |
| [abstract_small](abstract_small) | 168 | **168/168** | 1.1 s | 1.0 s | **−9.5%** |
| [abstract_medium](abstract_medium) | 92 | **92/92** | 98.6 s | 79.8 s | **−19.1%** |
| [abstract_large](abstract_large) | 232 | **232/232** | 603.1 s | 497.7 s | **−17.5%** |
| [abstract_hamiltonians](abstract_hamiltonians) | 400 | 399/400 ⁽²⁾ | 212.5 s | 190.3 s | **−10.4%** |
| **Total** | **1,023** | **1,021 (99.8%)** | **974.2 s** | **816.0 s** | **−16.2%** |

"Bit-identical" means all six gate/depth metrics **and** the full
`count_ops()` dict match exactly across main and PR for that circuit.
Per-circuit analysis lives in `<group>/analysis.txt`; raw data in
`<group>/{main,pr}.json`.

⁽¹⁾ `ham_mu_z_prime_enc_gray_dvalues_4-4-4-4` — Bug 2 (upstream).
⁽²⁾ `ham_ham_JW24` on all-to-all 24Q — Bug 2 (upstream).

### Top speedups per group (per-circuit)

| Group | Circuit | Main | PR | Speedup |
|---|---|---:|---:|---:|
| feynman | `hwb12` | 14.40 s | 10.85 s | **−24.7%** |
| feynman | `hwb11` | 6.76 s | 5.19 s | −23.2% |
| device_hamiltonians | `ham_gnp-k_5_n-90_rinst-02` | 0.43 s | 0.33 s | **−23.8%** |
| device_hamiltonians | `ham_ham_JW24` | 10.75 s | 8.42 s | −21.7% |
| abstract_small | `bb84_n8` (all topos) | ~0.002 s | ~0.001 s | **−21 to −24%** |
| abstract_medium | `factor247_n15-heavy-hex` | 14.33 s | 11.03 s | **−23.0%** |
| abstract_medium | `bwt_n21-linear` | 15.46 s | 11.92 s | −22.9% |
| abstract_large | `100-linear` | 6.39 s | 4.68 s | **−26.7%** |
| abstract_large | `vqe_uccsd_n28-square` | 18.09 s | 13.62 s | −24.7% |
| abstract_large | `bwt_n37-linear` | 67.38 s | 50.81 s | −24.6% |
| abstract_large | `multiplier_n400-linear` | 26.24 s | 19.91 s | −24.1% |
| abstract_hamiltonians | `ham_gnp-k_5_n-90_rinst-02-linear` | 0.75 s | 0.55 s | **−26.4%** |

Full per-circuit tables live in `<group>/analysis.txt` for each group.

---

## How this answers the "4 JSON files" ask

Matthew's review requested two experiments (one with stock benchpress
metrics, one with total-gate/depth metrics) × two branches = 4 JSON files
per circuit suite. Our single experiment produces the same information:
`benchpress_patch.diff` adds `output_gate_count_total`,
`output_gate_count_1q`, `output_depth_total`, and `output_depth_1q`
**alongside** the existing 2Q fields (it doesn't replace them). So each
per-group JSON in this tree contains all six metrics — both what stock
benchpress records and what the total-metric experiment would. Reviewers
can slice to 2Q-only or total at will.

We have 12 per-group JSONs (6 groups × 2 branches); they are the 4-JSONs
× N-groups equivalent of Matthew's ask.

---

## Environment

All measurements on a single Linux server (Intel Xeon SapphireRapids, 2×40
cores × 2 threads = 160 CPUs across 2 NUMA nodes). Both Qiskit builds made
from source with release + mimalloc:

```bash
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    pip install -e . --no-build-isolation
```

**Qiskit commits**: main = `c25216340`, PR = `5e077757e` (rebased onto main).
**Seed**: `QISKIT_TRANSPILER_SEED=1` in the shell for every pytest
invocation. (Due to an [upstream walrus-precedence bug](#bug-1), any non-empty
env-var value resolves to `seed_transpiler=1`; we set it to 1 explicitly to
document the effective seed.)

**Parallelization**: within each group, main and PR run in parallel on
separate NUMA nodes:

- main on node 0: `numactl --cpunodebind=0 --membind=0 pytest ...` (socket 0)
- PR on node 1:   `numactl --cpunodebind=1 --membind=1 pytest ...` (socket 1)

Groups themselves run sequentially so the two sockets are never contended
across groups. NUMA sweep in [`numa_sweep/`](numa_sweep) validated this
choice: single-socket pinning is within 1.7% of default-full-machine on
`hwb12`; worst-vs-best gap across 4 configs is 9.9%.

---

## Benchpress patch

[`scripts/benchpress_patch.diff`](scripts/benchpress_patch.diff) — two
changes to benchpress (to be applied against Qiskit/benchpress `main`):

1. `benchpress/qiskit_gym/utils/io.py`: add `output_gate_count_total`,
   `output_gate_count_1q`, `output_depth_total`, `output_depth_1q` to
   `extra_info`. Existing 2Q fields unchanged.
2. `benchpress/utilities/backends/flexible_backend.py`: add `seed=12345` to
   the `GenericBackendV2.__init__` call. Without this, each Python process
   instantiates `FlexibleBackend` with different random error rates — so
   the main and PR processes see different backend targets on the abstract
   tests, and SABRE's error-rate-weighted cost function produces different
   layouts. With the seed fix, abstract runs match bit-for-bit across
   branches (before the fix, 46–90% of abstract circuits mismatched; after
   the fix, the only mismatches are the 1 Bug 2 circuit in
   abstract_hamiltonians).

---

## Upstream bugs

Two independent Qiskit bugs surfaced during this investigation. Neither
is caused by PR #16157; both are pre-existing.

### Bug 1: walrus precedence in `QISKIT_TRANSPILER_SEED` parsing

File: `qiskit/transpiler/preset_passmanagers/generate_preset_pass_manager.py`

```python
# Current (buggy) — `:=` binds looser than `is not None`:
if seed := os.getenv("QISKIT_TRANSPILER_SEED", None) is not None:
    seed_transpiler = int(seed)

# Fix:
if (seed := os.getenv("QISKIT_TRANSPILER_SEED", None)) is not None:
    seed_transpiler = int(seed)
```

The current parse makes `seed` always a `bool`, so `int(seed)` is always 0
or 1 regardless of the env var's value. Same pattern in
`qiskit/compiler/transpiler.py:269`. Reproducer:
[`scripts/bug1_walrus_precedence.py`](scripts/bug1_walrus_precedence.py)
(pure Python, no Qiskit dependency). This will be filed upstream
separately from PR #16157.

### Bug 2: unseeded randomness in L2 pipeline

Even with `seed_transpiler` passed explicitly, two circuits out of 1,023
produce non-deterministic output across fresh Python subprocesses:
`ham_mu_z_prime_enc_gray_dvalues_4-4-4-4` (4Q, FakeTorino) and
`ham_ham_JW24` (24Q, all-to-all). Confirmed on both current main
(`c25216340`) and an older main commit (`03c640f73`).

**Key framing**: both **main** and **PR** produce the same distribution
of outputs on these circuits — they are equivalent rotational
decompositions of the same unitary, chosen by a tie-break that consumes
process-dependent state (likely `ahash` iteration order, or an unseeded
`RandomState`). 2Q gate count and 2Q depth are **identical** across
runs; only a small number of 1Q gates shift between equivalent
decompositions. In the single benchpress run that produced our data,
main happened to land on one decomposition for each circuit and PR
happened to land on the other — this is luck of the per-process hash
seed, not a PR-induced regression.

Reproducer: [`scripts/bug2_repro.py`](scripts/bug2_repro.py). Observed
rates: ~20–40% per call for mu_z_prime (N=10–20 reproduces both
buckets), ~0.25% per call for JW24 (needs ~300+ subprocesses or a
second full-suite run to observe both). Will be filed upstream.

---

## Directory layout

| Path | Contents |
|---|---|
| [`feynman/`](feynman), [`device_hamiltonians/`](device_hamiltonians), [`abstract_small/`](abstract_small), [`abstract_medium/`](abstract_medium), [`abstract_large/`](abstract_large), [`abstract_hamiltonians/`](abstract_hamiltonians) | Per-group: `main.json`, `pr.json`, `main.stdout`, `pr.stdout`, `environment.txt`, `analysis.txt` |
| [`determinism_envvar/`](determinism_envvar) | Early 3×3 determinism check on hwb12 (env-var seed only, before Bug 1 was identified) |
| [`numa_sweep/`](numa_sweep) | hwb12 under 4 CPU/memory configs × 3 reps |
| [`scripts/run_group.sh`](scripts/run_group.sh) | Per-group parallel-sockets launcher |
| [`scripts/run_queue.sh`](scripts/run_queue.sh), [`run_queue_abstract.sh`](scripts/run_queue_abstract.sh) | Sequential queue scripts used for this run |
| [`scripts/compare.py`](scripts/compare.py) | `main.json` vs `pr.json` analysis tool |
| [`scripts/benchpress_patch.diff`](scripts/benchpress_patch.diff) | Benchpress changes required |
| [`scripts/bug1_walrus_precedence.py`](scripts/bug1_walrus_precedence.py) | Reproducer for upstream Bug 1 |
| [`scripts/bug2_repro.py`](scripts/bug2_repro.py) | Reproducer for upstream Bug 2 |
| [`logs/queue.log`](logs/queue.log), [`queue_abstract.log`](logs/queue_abstract.log) | Queue execution logs |

---

## Step-by-step reproduction

The commands below assume a Linux host with two NUMA nodes. Adjust
`/mnt/data` paths to your own layout.

### 1. Build two Qiskit installs

```bash
# Main branch baseline (c25216340)
git clone https://github.com/Qiskit/qiskit.git /mnt/data/qiskit-main
cd /mnt/data/qiskit-main
git checkout c25216340
python3.11 -m venv /mnt/data/myenv-main
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    /mnt/data/myenv-main/bin/pip install -e . --no-build-isolation

# PR branch (5e077757e, rebased on c25216340)
git clone https://github.com/hfwen0502/qiskit.git /mnt/data/qiskit-pr
cd /mnt/data/qiskit-pr
git checkout 5e077757e   # or the latest test-3signal-only tip
python3.11 -m venv /mnt/data/myenv-pr
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    /mnt/data/myenv-pr/bin/pip install -e . --no-build-isolation

# Benchpress dependencies (both venvs)
/mnt/data/myenv-main/bin/pip install qiskit-ibm-runtime==0.46.1
/mnt/data/myenv-pr/bin/pip install   qiskit-ibm-runtime==0.46.1
# plus any benchpress test dependencies (pytest, pytest-benchmark fork, etc.)
```

### 2. Clone benchpress and apply the patch

```bash
git clone https://github.com/Qiskit/benchpress.git /mnt/data/benchpress
cd /mnt/data/benchpress
patch -p1 < /path/to/pr16157/scripts/benchpress_patch.diff
# verify:
grep -n "output_gate_count_total" benchpress/qiskit_gym/utils/io.py
grep -n "seed=12345" benchpress/utilities/backends/flexible_backend.py
```

### 3. Confirm CPU topology

```bash
lscpu | grep -E "Socket|NUMA|Core|Thread"
```

This should show 2 sockets / 2 NUMA nodes with nodes covering disjoint
CPU ranges. Our host: node 0 = CPUs 0-79 (socket 0, 40 cores × 2 threads),
node 1 = CPUs 80-159. The `numactl` invocations below assume that layout.

### 4. Run one group (illustrative: feynman)

Each group runs main and PR in parallel on separate sockets:

```bash
export QISKIT_TRANSPILER_SEED=1
cd /mnt/data/benchpress
OUT=/mnt/data/pr16157_evidence/feynman
mkdir -p "$OUT"

# main on NUMA 0
( source /mnt/data/myenv-main/bin/activate
  numactl --cpunodebind=0 --membind=0 \
      python -m pytest benchpress/qiskit_gym/device_transpile/test_feynman.py -q \
      --benchmark-json="$OUT/main.json" > "$OUT/main.stdout" 2>&1 ) &
MAIN=$!

# PR on NUMA 1
( source /mnt/data/myenv-pr/bin/activate
  numactl --cpunodebind=1 --membind=1 \
      python -m pytest benchpress/qiskit_gym/device_transpile/test_feynman.py -q \
      --benchmark-json="$OUT/pr.json" > "$OUT/pr.stdout" 2>&1 ) &
PR=$!

wait $MAIN; wait $PR
```

Or use the wrapped form: `bash scripts/run_group.sh feynman`.

### 5. Run all 6 groups

| # | Group | pytest target | Approx wall-clock (parallel) |
|---|---|---|---:|
| 1 | `feynman` | `benchpress/qiskit_gym/device_transpile/test_feynman.py` | ~3 min |
| 2 | `device_hamiltonians` | `benchpress/qiskit_gym/device_transpile/test_hamiltonians.py` | ~3 min |
| 3 | `abstract_small` | `benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_small` | ~3 min |
| 4 | `abstract_medium` | `benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_medium` | ~8 min |
| 5 | `abstract_large` | `benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py -k test_QASMBench_large` | ~45 min |
| 6 | `abstract_hamiltonians` | `benchpress/qiskit_gym/abstract_transpile/test_hamiltonians.py` | ~20 min |

The same invocation pattern as step 4 works for each — swap the pytest
target. Or use the queue script:

```bash
bash scripts/run_queue.sh   # device_hamiltonians → abstract_small → ... → abstract_hamiltonians
```

(Run feynman separately; the queue script handles the other five.)

### 6. Analyze each group

```bash
for g in feynman device_hamiltonians abstract_small abstract_medium \
         abstract_large abstract_hamiltonians; do
    python scripts/compare.py \
        /mnt/data/pr16157_evidence/$g/main.json \
        /mnt/data/pr16157_evidence/$g/pr.json \
        > /mnt/data/pr16157_evidence/$g/analysis.txt
done
```

Each `analysis.txt` lists: (a) correctness PASS/FAIL on the six metrics
and op dict, (b) total wall-clock ratio, (c) per-circuit slowdowns >5%,
(d) per-circuit speedups >5%.

### Expected results (match this tree)

- **feynman**: 50/50 bit-identical, −22.7% wall-clock, no slowdowns
- **device_hamiltonians**: 80/81 bit-identical (Bug 2 on
  `ham_mu_z_prime_enc_gray_dvalues_4-4-4-4`), −17.5%
- **abstract_small**: 168/168, −9.5%
- **abstract_medium**: 92/92, −19.1%
- **abstract_large**: 232/232, −17.5%
- **abstract_hamiltonians**: 399/400 (Bug 2 on `ham_ham_JW24-all-to-all`
  with ~0.25% frequency — ~1 in 400 runs shows the tie-break flip),
  −10.4%

If your results differ by more than the Bug 2 tie-break (2 circuits,
1Q-only drift) or by more than ±5% in aggregate timing, something about
the environment is different — start by confirming mimalloc is linked
and the seed env var is set.

### Run on your own hardware without two NUMA nodes

On a single-socket host, drop the `numactl` prefix and run main and PR
**sequentially** (otherwise they contend for cores and timing is noisy):

```bash
( source /mnt/data/myenv-main/bin/activate
  python -m pytest <target> --benchmark-json="$OUT/main.json" )
( source /mnt/data/myenv-pr/bin/activate
  python -m pytest <target> --benchmark-json="$OUT/pr.json" )
```

Gate/depth metrics (the no-regression claim) are **independent of
hardware and parallelism** — they depend only on Qiskit's deterministic
output for the fixed seed. Timing ratios may differ on other hardware.
