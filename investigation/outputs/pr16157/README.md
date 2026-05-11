# PR #16157: Replace Level 2 optimization loop's `FixedPoint` with a three-signal exit

Supporting data for
[qiskit#16157](https://github.com/Qiskit/qiskit/pull/16157). **Level 3
is unchanged.**

### AI/LLM disclosure

- [ ] I didn't use LLM tooling, or only used it privately.
- [x] I used the following tool to help write this PR description: **Claude Code (Sonnet / Opus class models)** — used to draft and iterate on this description, the evidence README, the benchpress patch, the per-group summary and reproducer scripts, and the analysis pipeline. A human reviewed and edited all output before submission.
- [x] I used the following tool to generate or modify code: **Claude Code** — used to assist with the Rust and Python changes in this PR (`commutation_cancellation.rs`, `remove_identity_equiv.rs`, `optimize_1q_gates_decomposition.rs`, `commutative_cancellation.py`, `remove_identity_equiv.py`, `builtin_plugins.py`). A human reviewed and verified every line.

### Reading this document

Path references like `scripts/compare.py` or `feynman/main.json` are
paths inside the attached evidence archive. After
`tar xf pr16157_evidence.tar.gz && cd pr16157`, all such paths resolve
locally. Nothing in this PR depends on any fork branch remaining
available.

## Summary

Across the qiskit_gym transpile suite of
[benchpress](https://github.com/Qiskit/benchpress) at
`optimization_level=2`, excluding 2 circuits affected by an upstream
Qiskit non-determinism (see [Upstream variability](#upstream-variability-circuits-excluded-from-analysis)):

- **All 1,021 remaining circuits produce bit-identical output** between
  main and PR on every gate/depth metric (2Q / 1Q / total × count /
  depth) and on the full `count_ops()` dict.
- **Every group faster**, aggregate **−16.2% wall-clock**; per-group
  speedups −9.5% to −22.7%.
- **Zero circuits slower on PR by >5%.**
- Matthew's stress test (`hwb12`, >1M gates entering the loop):
  **14.40 s → 10.85 s (−24.7%)**.

Generator for the tables below:
`scripts/summarize.py`.

---

## The three signals

The Level 2 optimization loop runs these passes in sequence and repeats
until a convergence check says to stop:

```
Optimize1qGatesDecomposition
CommutativeInverseCancellation
RemoveIdentityEquivalent
ConsolidateBlocks (idle wire contraction)
CommutativeCancellation
GatesInBasis → BasisTranslator → Optimize1qGatesDecomposition (if out-of-basis)
```

Stock Qiskit uses `FixedPoint("depth", "size")` — it runs the full loop
one extra confirmation iteration after depth and size stop changing.
This PR replaces that with three orthogonal boolean signals that
directly detect whether any pass created a new optimization opportunity
in the current iteration. If none fires, re-running the passes would
produce identical results → exit immediately, saving the extra iteration.

### Signal 1 — multi-qubit gate removed: `_opt_pass_changed`

Set when `CommutativeCancellation` or `RemoveIdentityEquivalent` deletes
a 2Q gate. Removing a 2Q gate causes the 1Q runs on either side to merge
into a longer single run that `Optimize1qGatesDecomposition` can
recombine into fewer gates.

**Code change — `CommutativeCancellation` (Rust):**

```rust
// crates/transpiler/src/passes/commutation_cancellation.rs
 pub fn cancel_commutations(
     dag: &mut DAGCircuit,
     ...
-) -> PyResult<()> {
+) -> PyResult<(bool, bool)> {
     ...
+    let mut changed = false;
+    let mut rotations_consolidated = false;
     for (cancel_key, cancel_set) in &cancellation_sets {
         if cancel_set.len() > 1 {
             if let GateOrRotation::Gate(g) = cancel_key.gate {
                 if SUPPORTED_GATES.contains(&g) {
+                    if cancel_key.qubits.len() > 1 {
+                        changed = true;
+                    }
                     for &c_node in &cancel_set[0..(cancel_set.len() / 2) * 2] {
                         dag.remove_op_node(c_node);
                     }
 ...
-    Ok(())
+    Ok((changed, rotations_consolidated))
 }
```

**Code change — `RemoveIdentityEquivalent` (Rust):**

```rust
// crates/transpiler/src/passes/remove_identity_equiv.rs
 pub fn run_remove_identity_equiv(
     dag: &mut DAGCircuit,
     approx_degree: Option<f64>,
     target: Option<&Target>,
-) -> PyResult<()> {
+) -> PyResult<bool> {
     ...
+    let mut multi_qubit_changed = false;
     for (node, phase_update) in remove_list {
+        if dag.get_qargs(dag[node].unwrap_operation().qubits).len() > 1 {
+            multi_qubit_changed = true;
+        }
         dag.remove_op_node(node);
         dag.add_global_phase(&Param::Float(phase_update))
             .expect("The global phase is guaranteed to be a float");
     }
-    Ok(())
+    Ok(multi_qubit_changed)
 }
```

**Python wiring** — `CommutativeCancellation.run` and
`RemoveIdentityEquivalent.run` now set `property_set["_opt_pass_changed"]`
from the Rust return value.

### Signal 2 — rotations consolidated: `_opt_1q_consolidated`

Set when `CommutativeCancellation` merges adjacent same-axis rotations.
This *shortens* the 1Q runs, giving `Optimize1qGatesDecomposition` new,
shorter sequences to recombine (it can now reach beyond what it had
previously consolidated). Only fires once per iteration since
consolidation is idempotent.

**Code change** — uses the second bool added to `cancel_commutations`
above:

```python
# qiskit/transpiler/passes/optimization/commutative_cancellation.py
 def run(self, dag):
-    commutation_cancellation.cancel_commutations(
-        dag, self._commutation_checker, sorted(self.basis)
+    multi_qubit_changed, rotations_consolidated = (
+        commutation_cancellation.cancel_commutations(
+            dag, self._commutation_checker, sorted(self.basis)
+        )
     )
+    if multi_qubit_changed:
+        self.property_set["_opt_pass_changed"] = True
+    if rotations_consolidated:
+        self.property_set["_opt_1q_consolidated"] = True
     return dag
```

### Signal 3 — out-of-basis gate produced: `all_gates_in_basis == False`

Uses the existing `GatesInBasis` analysis pass that already runs in the
L2 loop. When `CommutativeCancellation` consolidates rotations it can
produce a gate outside the target basis; `BasisTranslator` then runs to
translate it, emitting an unoptimized sequence that requires another
`Optimize1qGatesDecomposition` pass. **No code change here** — the
signal is already available; it just becomes a loop-exit trigger.

### Loop condition wiring

The new loop-exit helper registers a reset-pass that clears the two
boolean signals at the start of each iteration, and a check function
that re-iterates iff any of the three signals fires:

```python
# qiskit/transpiler/preset_passmanagers/builtin_plugins.py
+def _optimization_check_changed_flag():
+    """Loop condition based on per-pass changed flags."""
+    from qiskit.transpiler.basepasses import AnalysisPass
+
+    class _ResetChangedFlag(AnalysisPass):
+        def run(self, dag):
+            self.property_set["_opt_pass_changed"] = False
+            self.property_set["_opt_1q_consolidated"] = False
+
+    def check(property_set):
+        return (
+            property_set.get("_opt_pass_changed", False)
+            or property_set.get("_opt_1q_consolidated", False)
+            or not property_set.get("all_gates_in_basis", True)
+        )
+
+    return (_ResetChangedFlag(), check)

 class OptimizationPassManager(PassManagerStagePlugin):
     ...
     case 2:
-        loop_check, continue_loop = _optimization_check_fixed_point()
+        reset_changed, continue_loop = _optimization_check_changed_flag()
+        loop = [reset_changed] + loop
+        loop_check = []
     case 3:
         # (unchanged: keeps FixedPoint / MinimumPoint logic)
```

### Why these three signals are sufficient

Inside the L2 loop, new optimization opportunities can arise only
through one of three mechanisms:

1. Removing a 2Q gate merges adjacent 1Q runs → Signal 1.
2. Merging rotations shortens a 1Q run → Signal 2.
3. Producing an out-of-basis gate triggers `BasisTranslator`, which
   emits an unoptimized sequence → Signal 3.

`Optimize1qGatesDecomposition` always rewrites the canonical form of 1Q
runs it encounters, so it's stateless across invocations: if no pass
changed the 1Q runs around it, a second invocation yields the same DAG.
Tracking "did Optimize1q change anything" would be a lagging indicator
(it mutates unconditionally), so we track the *upstream* mechanisms
that can give Optimize1q new material — which is exactly the three
signals above.

`FixedPoint` runs one confirmation iteration after depth + size stop
changing; with our signals the same information is available from the
passes themselves. Eliminating that extra iteration is the source of
the speedup.

---

## Upstream variability (circuits excluded from analysis)

During this work we identified a **pre-existing Qiskit non-determinism**
unrelated to this PR: 2 circuits out of 1,023 produce different outputs
across identical subprocess runs, even with `seed_transpiler` passed
explicitly. 2Q gate counts are identical across runs; only a small
number of 1Q gates shift between equivalent rotational decompositions —
evidence of a tie-break in a 1Q rotation pass that consumes
process-dependent state (likely `ahash` iteration order, or an unseeded
`RandomState`).

Both **main** and **PR** produce the same distribution of outputs on
these circuits — they are equivalent decompositions of the same
unitary, picked by an unseeded tie-break. In the single benchpress run
that produced our data, main and PR happened to land on different
buckets for each circuit, which would show up in a naive diff as "PR
has +N 1Q gates here". It is **not** a PR-induced regression — either
branch can produce either output with some probability on a given run.

**Affected circuits** (removed from the per-group aggregate tables below):

| Group | Circuit | 2Q count (both branches) | Outputs observed |
|---|---|---:|---|
| device_hamiltonians | `ham_mu_z_prime_enc_gray_dvalues_4-4-4-4` | 4 | A: `{rz:17, sx:16, cz:4}` (37, depth 11) vs B: `{rz:16, sx:16, cz:4, x:2}` (38, depth 14). Observed ~20–40% of runs in either bucket. |
| abstract_hamiltonians | `ham_ham_JW24-all-to-all` | 46,079 | A: `{rz:59345, sx:58910, cz:46079, x:5552}` vs B: `{rz:59350, sx:58914, cz:46079, x:5550}`. Observed ~0.25% in either bucket (rare). |

**Reproducer**: `scripts/bug2_repro.py`.
Run `~/.venv-main/bin/python scripts/bug2_repro.py 20` on a Qiskit-main
install. For `mu_z_prime` both buckets appear in ≈10–20 fresh
subprocesses; for `JW24` the bucket flip is rare (~1/400), easiest to
reproduce by running the benchpress `abstract_transpile/test_hamiltonians.py`
suite twice on the same branch and diffing the two JSON files.

Verified on current main (`c25216340`) and on the older main commit
`03c640f73`. Not introduced by recent commits; will be filed upstream
separately.

---

## Per-group results

Each group below shows:

- **What it covers** (benchpress test file + description)
- **Pytest command** used to produce the data
- **Aggregate main vs PR table**: gate count (2Q / 1Q / total) + depth
  (2Q / 1Q / total) + wall-clock
- **Data files** in this tree

Tables generated by `scripts/summarize.py`;
re-run it from this directory to regenerate.

Environment identical across all groups: Qiskit main `c25216340` vs PR
`5e077757e` (rebased onto main), both built release + mimalloc; seed
`QISKIT_TRANSPILER_SEED=1`; main on NUMA node 0, PR on NUMA node 1, run
in parallel. See [Environment and reproduction](#environment-and-reproduction)
for the full shell recipe.

### 1. feynman — `device_transpile/test_feynman.py`

Feynman project QASM files at `benchpress/qasm/feynman/` compiled for
FakeTorino (133Q heavy-hex). Includes the `hwb8`/`hwb10`/`hwb11`/`hwb12`
family (Matthew's stress test).

```bash
pytest benchpress/qiskit_gym/device_transpile/test_feynman.py -q \
    --benchmark-json=feynman/main.json  # (or pr.json)
```

**50 circuits** (3 too large for FakeTorino are `pytest.skip`'d on both
branches):

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 1,149,930 | 1,149,930 | **0 (+0.00%)** |
| 1Q gate count | 3,154,651 | 3,154,651 | **0 (+0.00%)** |
| Total gate count | 4,304,581 | 4,304,581 | **0 (+0.00%)** |
| 2Q depth | 694,307 | 694,307 | **0 (+0.00%)** |
| 1Q depth | 1,226,805 | 1,226,805 | **0 (+0.00%)** |
| Total depth | 1,907,248 | 1,907,248 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 24.76 | 19.14 | **−22.7%** |

Top speedups: `hwb12` −24.7% (14.40 s → 10.85 s), `hwb11` −23.2%,
`hwb8` −19.7%, `hwb10` −15.7%.

Data: `feynman/main.json`,
`feynman/pr.json`,
`feynman/analysis.txt`.

### 2. device_hamiltonians — `device_transpile/test_hamiltonians.py`

HamLib Hamiltonians from `benchpress/hamiltonian/hamlib/100_representative.json`
compiled as Trotter circuits for FakeTorino.

```bash
pytest benchpress/qiskit_gym/device_transpile/test_hamiltonians.py -q \
    --benchmark-json=device_hamiltonians/main.json
```

**80 circuits** (19 too large for FakeTorino are skipped; 1 Bug 2 circuit
`ham_mu_z_prime_enc_gray_dvalues_4-4-4-4` excluded from the aggregate):

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 889,535 | 889,535 | **0 (+0.00%)** |
| 1Q gate count | 2,571,738 | 2,571,738 | **0 (+0.00%)** |
| Total gate count | 3,461,273 | 3,461,273 | **0 (+0.00%)** |
| 2Q depth | 692,118 | 692,118 | **0 (+0.00%)** |
| 1Q depth | 1,263,538 | 1,263,538 | **0 (+0.00%)** |
| Total depth | 1,943,544 | 1,943,544 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 34.06 | 28.09 | **−17.5%** |

Top speedups: `ham_gnp-k_5_n-90_rinst-02` −23.8%, `ham_ham_JW24` −21.7%
(10.75 s → 8.42 s), `ham_ham_BK22` −20.6%, `ham_ham_JW-22` −19.9%.

Data: `device_hamiltonians/main.json`,
`device_hamiltonians/pr.json`,
`device_hamiltonians/analysis.txt`.

### 3. abstract_small — `abstract_transpile/test_qasmbench.py -k Small`

QASMBench Small suite × 4 abstract topologies (all-to-all, square,
heavy-hex, linear), compiled via benchpress's `FlexibleBackend` (patched
to use a fixed error-rate seed — see
[Benchpress patch](#benchpress-patch)).

```bash
pytest benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py \
    -k test_QASMBench_small -q \
    --benchmark-json=abstract_small/main.json
```

**168 circuits**:

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 29,383 | 29,383 | **0 (+0.00%)** |
| 1Q gate count | 91,401 | 91,401 | **0 (+0.00%)** |
| Total gate count | 120,784 | 120,784 | **0 (+0.00%)** |
| 2Q depth | 27,656 | 27,656 | **0 (+0.00%)** |
| 1Q depth | 59,422 | 59,422 | **0 (+0.00%)** |
| Total depth | 86,986 | 86,986 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 1.11 | 1.00 | **−9.5%** |

Top speedups: `bb84_n8` across topologies (−21% to −24%),
`variational_n4-linear` −20.4%.

Data: `abstract_small/main.json`,
`abstract_small/pr.json`,
`abstract_small/analysis.txt`.

### 4. abstract_medium — `abstract_transpile/test_qasmbench.py -k Medium`

QASMBench Medium suite × 4 topologies.

```bash
pytest benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py \
    -k test_QASMBench_medium -q \
    --benchmark-json=abstract_medium/main.json
```

**92 circuits**:

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 3,357,983 | 3,357,983 | **0 (+0.00%)** |
| 1Q gate count | 10,344,081 | 10,344,081 | **0 (+0.00%)** |
| Total gate count | 13,702,064 | 13,702,064 | **0 (+0.00%)** |
| 2Q depth | 2,395,414 | 2,395,414 | **0 (+0.00%)** |
| 1Q depth | 5,313,597 | 5,313,597 | **0 (+0.00%)** |
| Total depth | 7,676,087 | 7,676,087 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 98.56 | 79.77 | **−19.1%** |

Top speedups: `factor247_n15-heavy-hex` −23.0% (14.33 s → 11.03 s),
`bwt_n21-linear` −22.9%, `factor247_n15-linear` −22.6%.

Data: `abstract_medium/main.json`,
`abstract_medium/pr.json`,
`abstract_medium/analysis.txt`.

### 5. abstract_large — `abstract_transpile/test_qasmbench.py -k Large`

QASMBench Large suite × 4 topologies. The long tail of the run;
`bwt_n37`, `multiplier_n400`, `vqe_uccsd_n28` dominate wall-clock.

```bash
pytest benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py \
    -k test_QASMBench_large -q \
    --benchmark-json=abstract_large/main.json
```

**232 circuits**:

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 22,517,490 | 22,517,490 | **0 (+0.00%)** |
| 1Q gate count | 59,439,157 | 59,439,157 | **0 (+0.00%)** |
| Total gate count | 81,956,647 | 81,956,647 | **0 (+0.00%)** |
| 2Q depth | 11,902,815 | 11,902,815 | **0 (+0.00%)** |
| 1Q depth | 22,225,009 | 22,225,009 | **0 (+0.00%)** |
| Total depth | 33,980,864 | 33,980,864 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 603.10 | 497.66 | **−17.5%** |

Top speedups: `100-linear` −26.7%, `vqe_uccsd_n28-square` −24.7%
(18.09 s → 13.62 s), `bwt_n37-linear` −24.6% (67.38 s → 50.81 s),
`multiplier_n400-linear` −24.1%.

Data: `abstract_large/main.json`,
`abstract_large/pr.json`,
`abstract_large/analysis.txt`.

### 6. abstract_hamiltonians — `abstract_transpile/test_hamiltonians.py`

HamLib Hamiltonians × 4 abstract topologies.

```bash
pytest benchpress/qiskit_gym/abstract_transpile/test_hamiltonians.py -q \
    --benchmark-json=abstract_hamiltonians/main.json
```

**399 circuits** (1 Bug 2 circuit `ham_ham_JW24-all-to-all` excluded):

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 4,844,869 | 4,844,869 | **0 (+0.00%)** |
| 1Q gate count | 13,164,474 | 13,164,474 | **0 (+0.00%)** |
| Total gate count | 18,009,343 | 18,009,343 | **0 (+0.00%)** |
| 2Q depth | 3,052,128 | 3,052,128 | **0 (+0.00%)** |
| 1Q depth | 5,328,520 | 5,328,520 | **0 (+0.00%)** |
| Total depth | 8,335,509 | 8,335,509 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 207.69 | 186.40 | **−10.3%** |

Top speedups: `ham_gnp-k_5_n-90_rinst-02-linear` −26.4%,
`ham_gnp-k_5_n-60_rinst-19-linear` −25.9%,
`ham_ham_JW-18-heavy-hex` −22.3%.

Data: `abstract_hamiltonians/main.json`,
`abstract_hamiltonians/pr.json`,
`abstract_hamiltonians/analysis.txt`.

### Total across all 6 groups

**1,021 circuits** (2 Bug 2 circuits excluded):

| Metric | Main | PR | Δ (PR − Main) |
|---|---:|---:|---:|
| 2Q gate count | 32,789,190 | 32,789,190 | **0 (+0.00%)** |
| 1Q gate count | 88,765,502 | 88,765,502 | **0 (+0.00%)** |
| Total gate count | 121,554,692 | 121,554,692 | **0 (+0.00%)** |
| 2Q depth | 18,764,438 | 18,764,438 | **0 (+0.00%)** |
| 1Q depth | 35,416,891 | 35,416,891 | **0 (+0.00%)** |
| Total depth | 53,930,238 | 53,930,238 | **0 (+0.00%)** |
| Wall-clock (sum of means, s) | 969.27 | 812.05 | **−16.2%** |

---

## Environment and reproduction

### Host

Linux server, Intel Xeon SapphireRapids, 2×40 cores × 2 threads = 160
CPUs across 2 NUMA nodes. Python 3.11 on the server. We measured on
this host; gate/depth metrics are independent of hardware, wall-clock
numbers will vary.

### 1. Build both Qiskit installs

```bash
# Main branch baseline (c25216340)
git clone https://github.com/Qiskit/qiskit.git /mnt/data/qiskit-main
cd /mnt/data/qiskit-main
git checkout c25216340
python3.11 -m venv /mnt/data/myenv-main
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    /mnt/data/myenv-main/bin/pip install -e . --no-build-isolation

# PR branch (5e077757e, test-3signal-only on the fork, rebased onto c25216340)
git clone https://github.com/hfwen0502/qiskit.git /mnt/data/qiskit-pr
cd /mnt/data/qiskit-pr
git checkout test-3signal-only
python3.11 -m venv /mnt/data/myenv-pr
CC=/usr/bin/cc QISKIT_BUILD_WITH_MIMALLOC=1 QISKIT_BUILD_PROFILE=release \
    /mnt/data/myenv-pr/bin/pip install -e . --no-build-isolation

# Both venvs need qiskit-ibm-runtime for FakeTorino and benchpress test deps
for v in /mnt/data/myenv-main /mnt/data/myenv-pr; do
    $v/bin/pip install qiskit-ibm-runtime==0.46.1
done
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

### 3. Run one group (main on socket 0, PR on socket 1, in parallel)

```bash
export QISKIT_TRANSPILER_SEED=1
OUT=/mnt/data/pr16157_evidence/feynman
mkdir -p "$OUT"
cd /mnt/data/benchpress

( source /mnt/data/myenv-main/bin/activate
  numactl --cpunodebind=0 --membind=0 \
      python -m pytest benchpress/qiskit_gym/device_transpile/test_feynman.py -q \
      --benchmark-json="$OUT/main.json" > "$OUT/main.stdout" 2>&1 ) &
( source /mnt/data/myenv-pr/bin/activate
  numactl --cpunodebind=1 --membind=1 \
      python -m pytest benchpress/qiskit_gym/device_transpile/test_feynman.py -q \
      --benchmark-json="$OUT/pr.json" > "$OUT/pr.stdout" 2>&1 ) &
wait
```

Wrapped: `bash scripts/run_group.sh feynman`. All 5 non-feynman groups
can be queued sequentially with `bash scripts/run_queue.sh`.

### 4. Analyze

```bash
# Per-circuit analysis (PASS/FAIL + slowdowns + speedups)
python scripts/compare.py feynman/main.json feynman/pr.json > feynman/analysis.txt

# Aggregate tables across all 6 groups (regenerates the tables in this README)
python scripts/summarize.py .
```

### Single-socket / laptop hosts

Drop `numactl` and run main and PR sequentially (otherwise they contend
for cores and timing is noisy). Gate/depth metrics are unaffected by
parallelism or hardware.

---

## Benchpress patch

`scripts/benchpress_patch.diff` — two
changes to a fresh Qiskit/benchpress `main` checkout:

1. **`benchpress/qiskit_gym/utils/io.py`** — add
   `output_gate_count_total`, `output_gate_count_1q`,
   `output_depth_total`, `output_depth_1q` to each test's `extra_info`.
   Existing 2Q fields unchanged. Covers Matthew's ask for total gate
   count + overall depth.
2. **`benchpress/utilities/backends/flexible_backend.py`** — add
   `seed=12345` to the `GenericBackendV2.__init__` call. Without this,
   each Python process generates different random error rates, so main
   and PR processes on the abstract tests see different backend targets
   and SABRE's error-rate-weighted cost function picks different
   layouts. With the seed fix, abstract runs match bit-for-bit across
   branches.

---

## Upstream Qiskit bug 1 (walrus precedence in `QISKIT_TRANSPILER_SEED`)

Separate from the Bug 2 non-determinism above. In
`qiskit/transpiler/preset_passmanagers/generate_preset_pass_manager.py`:

```python
# Current (buggy) — `:=` binds looser than `is not None`:
if seed := os.getenv("QISKIT_TRANSPILER_SEED", None) is not None:
    seed_transpiler = int(seed)

# Fix:
if (seed := os.getenv("QISKIT_TRANSPILER_SEED", None)) is not None:
    seed_transpiler = int(seed)
```

With the current parse, `seed` is always a `bool`, so `int(seed)` is
always 0 or 1 regardless of the env-var value. Same pattern at
`qiskit/compiler/transpiler.py:269`. Pure-Python reproducer:
`scripts/bug1_walrus_precedence.py`.
Will be filed upstream separately. For this evidence we set
`QISKIT_TRANSPILER_SEED=1` to document the effective seed honestly —
and because both branches hit the bug identically, comparisons between
them are fair regardless.

---

## Directory layout

| Path | Contents |
|---|---|
| `feynman/`, `device_hamiltonians/`, `abstract_small/`, `abstract_medium/`, `abstract_large/`, `abstract_hamiltonians/` | Per-group: `main.json`, `pr.json`, `main.stdout`, `pr.stdout`, `environment.txt`, `analysis.txt` |
| `determinism_envvar/` | Early 3×3 determinism check on hwb12 (env-var seed only, before Bug 1 was identified) |
| `numa_sweep/` | hwb12 under 4 CPU/memory configs × 3 reps |
| `scripts/run_group.sh` | Per-group parallel-sockets launcher |
| `scripts/run_queue.sh`, `run_queue_abstract.sh` | Sequential queue scripts used for this run |
| `scripts/compare.py` | Per-circuit `main.json` vs `pr.json` analysis tool |
| `scripts/summarize.py` | Per-group aggregate summary (regenerates the tables in this README, excludes Bug 2 circuits) |
| `scripts/benchpress_patch.diff` | Benchpress changes required |
| `scripts/bug1_walrus_precedence.py` | Reproducer for upstream Bug 1 |
| `scripts/bug2_repro.py` | Reproducer for the 2 upstream-variability circuits |
| `logs/queue.log`, `queue_abstract.log` | Queue execution logs |
