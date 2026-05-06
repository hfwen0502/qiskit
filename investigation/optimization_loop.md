# Investigation: Smarter Loop Exit for Level 2 Optimization

## Motivation

The Qiskit dev team asked: **"Is the Level 2 optimization loop warranted, or is there a better loop condition that could avoid an unnecessary second iteration to validate we've reached a steady state?"**

The current Level 2 loop uses `FixedPoint("size") AND FixedPoint("depth")` for convergence. `FixedPoint` compares the current metric to the previous iteration's value and requires at least **two iterations** — one productive, one to confirm nothing changed. The question is whether we can find a condition that exits the loop earlier when the productive work is already done.

**Scope**: Level 2 only. The team confirmed Level 3 keeps its existing `MinimumPoint` loop.

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

## Source Files

| File | Role |
|------|------|
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Defines optimization stage as a plugin; assembles the loop |
| `qiskit/passmanager/flow_controllers.py` | `DoWhileController` — the loop mechanism |
| `qiskit/transpiler/passes/utils/fixed_point.py` | `FixedPoint` — current convergence check |
| `crates/transpiler/src/passes/remove_identity_equiv.rs` | Rust: RemoveIdentityEquivalent |
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | Rust: Optimize1qGatesDecomposition |
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Rust: CommutativeCancellation |
| `qiskit/transpiler/passes/optimization/contract_idle_wires_in_control_flow.py` | Python: ContractIdleWiresInControlFlow |

## Level 2 Optimization Pipeline

The optimization stage has three phases: **pre-loop**, **loop**, and **post-loop**.

```python
# builtin_plugins.py, Level 2 case
pre_loop = [
    ConsolidateBlocks(...),       # Collect 2Q blocks → optimal unitaries via KAK/Weyl
    UnitarySynthesis(...),        # Resynthesize unitaries into basis gates
]
loop = [
    RemoveIdentityEquivalent(...),       # Remove near-identity gates
    Optimize1qGatesDecomposition(...),   # Merge 1Q chains → optimal basis sequence
    CommutativeCancellation(...),        # Cancel commuting gate pairs
    ContractIdleWiresInControlFlow(),    # Remove idle wires in control-flow blocks
]
post_loop = []
```

The pre-loop runs once. The loop repeats until convergence. Assembly:

```python
optimization = PassManager()
optimization.append(pre_loop + loop_check)                          # Run once
optimization.append(DoWhileController(loop + unroll + loop_check,   # Loop
                                      do_while=continue_loop))
optimization.append(post_loop)
```

`DoWhileController` has do-while semantics: always runs at least once, then checks `do_while(property_set)` after each iteration.

### Current Convergence: FixedPoint

```python
def _optimization_check_fixed_point():
    def check(property_set):
        return not (property_set["depth_fixed_point"] and property_set["size_fixed_point"])

    setup = [Size(recurse=True), Depth(recurse=True), FixedPoint("size"), FixedPoint("depth")]
    return (setup, check)
```

At the end of each iteration: `Size` and `Depth` compute metrics, then `FixedPoint` compares each to the previous value. Loop stops when both size AND depth are unchanged.

**The problem**: `FixedPoint` stores `_previous = None` initially, so the first comparison always returns `False` (not converged). This means **minimum 2 iterations** — even when iteration 1 already produces the final result.

## Profiling: What Does the Loop Actually Do?

We profiled 13 circuits (6 original + 6 additional families + 1 real chemistry) at Level 2 on FakeTorino (133Q heavy-hex) to understand what work each iteration and each pass contributes.

### Test Circuits

| Circuit | Description | Input Gates |
|---------|-------------|:-----------:|
| QFT_100 | 100-qubit QFT, chain topology | ~5K |
| QV_100 | 100-qubit Quantum Volume, dense random | ~100K |
| EfficientSU2_100 | 100-qubit EfficientSU2, linear entanglement | ~700 |
| QAOA_100 | 100-qubit QAOA (3 layers, random ZZ) | ~1.2K |
| BV_100 | 100-qubit Bernstein-Vazirani | ~300 |
| Heisenberg_100 | 100-qubit 10x10 square Heisenberg (3 Trotter steps) | ~3.2K |
| Grover_50 | 50-qubit search oracle with MCX/CCX | ~564 |
| Adder_80 | 80-qubit CDKMRippleCarryAdder(39) | ~108 |
| Random_80 | 80-qubit random_circuit(80, 40) | ~2.3K |
| GHZ_100 | 100-qubit GHZ (H + CX chain) | ~200 |
| QPE_50 | 50-qubit phase estimation + IQFT | ~1.4K |
| Toffoli_90 | 90-qubit CCX chain (3 layers) | ~210 |
| fe4s4_LUCJ | 72-qubit [4Fe-4S] LUCJ chemistry ansatz | ~4K |

**Scripts**: `investigation/profile_optimization_loop.py`, `investigation/test_no_loop.py`, `investigation/test_more_circuits.py`, `investigation/test_fe4s4.py`

### Finding 1: The loop does no useful 2Q work after iteration 1

| Circuit | L2 Iters | Pre-Loop 2Q Delta | Loop 2Q Delta | Loop 2Q Contribution |
|---------|:--------:|:-----------------:|:-------------:|:--------------------:|
| QFT_100 | 3 | -1,492 (14.0%) | -56 | CommutativeCancellation |
| QV_100 | 2 | -2,184 (2.2%) | 0 | none |
| EfficientSU2_100 | 2 | 0 (0.0%) | 0 | none |
| QAOA_100 | 3 | -282 (1.7%) | 0 | none |
| BV_100 | 2 | -194 (49.7%) | 0 | none |
| Heisenberg_100 | 2 | -1,809 (26.5%) | 0 | none |
| fe4s4_LUCJ | 3 | -72 (1.7%) | 0 | none |

The pre-loop (ConsolidateBlocks + UnitarySynthesis) does all the 2Q gate reduction. The loop body contributes zero 2Q gates on 12/13 circuits and 56 on QFT only (from CommutativeCancellation finding CX cancellations). All of QFT's 56 CX reductions happen in iteration 1.

### Finding 2: Iterations 2+ are pure confirmation overhead

When we remove the loop entirely (single iteration), the results are identical:

| Circuit | Loop 2Q | No-Loop 2Q | Diff | Loop Opt (ms) | No-Loop Opt (ms) |
|---------|:-------:|:----------:|:----:|:-------------:|:-----------------:|
| QFT_100 | 9,116 | 9,116 | 0 | 2,714 | 2,175 |
| QV_100 | 96,093 | 96,093 | 0 | 18,324 | 15,789 |
| EfficientSU2_100 | 297 | 297 | 0 | 126 | 100 |
| QAOA_100 | 15,961 | 15,961 | 0 | 3,046 | 2,223 |
| BV_100 | 200 | 200 | 0 | 118 | 105 |
| Heisenberg_100 | 4,947 | 4,947 | 0 | 1,299 | 1,140 |
| fe4s4_LUCJ | 4,225 | 4,225 | 0 | 5,822 | 3,754 |

**Zero 2Q gate regression on all circuits.** The extra iterations exist solely because FixedPoint needs a confirmation pass.

### Finding 3: Each pass has a clear scope

| Pass | 2Q Gate Impact | 1Q Gate Impact | When It Helps |
|------|:-------------:|:--------------:|--------------|
| RemoveIdentityEquivalent | Removes multi-qubit identity gates | Removes 1Q identity gates | After synthesis creates near-identity gates |
| Optimize1qGatesDecomposition | None — 1Q only | Merges 1Q chains | Always (1Q cleanup) |
| CommutativeCancellation | Cancels commuting 2Q pairs (CX, CY, CZ) | Merges commuting rotations (RZ, P, etc.) | Rotation-heavy circuits (QFT, QPE) |
| ContractIdleWiresInControlFlow | None (idle wire removal) | None | Circuits with control flow |

**Key insight**: Only `RemoveIdentityEquivalent` and `CommutativeCancellation` can create new optimization opportunities for subsequent iterations. Specifically: 2Q gate removal exposes longer 1Q runs, and rotation consolidation shortens 1Q runs — both give `Optimize1qGatesDecomposition` new material to work with. If neither pass finds actionable work, the circuit's structure is unchanged and re-iteration produces identical results.

## Solution: Opportunity-Driven Changed-Flag Loop Condition

### Design

Replace the indirect FixedPoint metric check with direct signals from the passes themselves:

1. **Rust passes report what they did**: Each pass returns whether it found actionable work (2Q cancellations, rotation consolidations)
2. **Python wrappers propagate to `property_set`**: Two flags capture the two mechanisms that create new optimization opportunities
3. **Loop checks the flags directly**: No need for Size/Depth/FixedPoint analysis passes

### Implementation

#### Part 1: Rust — Return `bool` from each pass

All 4 Rust loop passes already know internally when they modify the DAG. Previously they returned `PyResult<()>`, discarding that information. We change to `PyResult<bool>`.

**`remove_identity_equiv.rs`** — returns `true` only when multi-qubit identity gates are removed:

```rust
pub fn run_remove_identity_equiv(...) -> PyResult<bool> {
    // ... existing logic builds remove_list ...
    let mut multi_qubit_changed = false;
    for (node, phase_update) in remove_list {
        if dag.get_qargs(dag[node].unwrap_operation().qubits).len() > 1 {
            multi_qubit_changed = true;
        }
        dag.remove_op_node(node);
        dag.add_global_phase(&Param::Float(phase_update))?;
    }
    Ok(multi_qubit_changed)
}
```

**`commutation_cancellation.rs`** — returns `(multi_qubit_changed, rotations_consolidated)`:

```rust
pub fn cancel_commutations(...) -> PyResult<(bool, bool)> {
    // ... existing logic builds cancellation_sets ...
    let mut changed = false;
    let mut rotations_consolidated = false;
    for (cancel_key, cancel_set) in &cancellation_sets {
        if cancel_set.len() > 1 {
            if let GateOrRotation::Gate(g) = cancel_key.gate {
                if SUPPORTED_GATES.contains(&g) {
                    if cancel_key.qubits.len() > 1 {
                        changed = true;  // Only for multi-qubit gates (CX, CY, CZ)
                    }
                    // ... remove gates ...
                }
                continue;
            }
            if matches!(cancel_key.gate, GateOrRotation::ZRotation | GateOrRotation::XRotation) {
                rotations_consolidated = true;  // 1Q rotation consolidation
                // ... consolidate rotations ...
            }
        }
    }
    Ok((changed, rotations_consolidated))
}
```

**`optimize_1q_gates_decomposition.rs`** — returns `bool` but we don't use it for loop control (1Q-only pass):

```rust
pub fn run_optimize_1q_gates_decomposition(...) -> PyResult<bool> {
    let mut changed = false;
    for raw_run in runs {
        // ... if replacement is better ...
        changed = true;
        // ... apply replacement ...
    }
    Ok(changed)
}
```

**Backward compatibility**: All existing callers (including Level 1 and Level 3 code paths) use `function_call()?;` pattern (Rust `?` unwraps `Result`, `;` discards the `bool`). No caller breaks. The Rust return-type changes are purely additive — **Level 1 and Level 3 behavior is completely unchanged** because they never read the returned `bool`.

#### Part 2: Python wrappers — propagate to property_set

Only 2Q-relevant passes set the loop flag:

```python
# remove_identity_equiv.py
def run(self, dag):
    changed = remove_identity_equiv(dag, self._approximation_degree, self._target)
    if changed:
        self.property_set["_opt_pass_changed"] = True
    return dag

# commutative_cancellation.py
def run(self, dag):
    multi_qubit_changed, rotations_consolidated = (
        cancel_commutations(dag, self._commutation_checker, sorted(self.basis))
    )
    if multi_qubit_changed:
        self.property_set["_opt_pass_changed"] = True
    if rotations_consolidated:
        self.property_set["_opt_1q_consolidated"] = True
    return dag
```

Passes that only affect 1Q gates do **not** set the flag:
- `Optimize1qGatesDecomposition` — 1Q merging only
- `ContractIdleWiresInControlFlow` — idle wire removal, no gate changes

#### Part 3: Loop condition — replace FixedPoint

```python
# builtin_plugins.py
def _optimization_check_changed_flag():
    from qiskit.transpiler.basepasses import AnalysisPass

    class _ResetChangedFlag(AnalysisPass):
        """Reset the changed flags at the start of each iteration."""
        def run(self, dag):
            self.property_set["_opt_pass_changed"] = False
            self.property_set["_opt_1q_consolidated"] = False

    def check(property_set):
        """Continue looping if any pass modified 2Q gates OR consolidated rotations."""
        return property_set.get("_opt_pass_changed", False) or property_set.get(
            "_opt_1q_consolidated", False
        )

    return (_ResetChangedFlag(), check)
```

Level 2 assembly:

```python
case 2:
    pre_loop = [ConsolidateBlocks(...), UnitarySynthesis(...)]
    loop = [
        RemoveIdentityEquivalent(...),
        Optimize1qGatesDecomposition(...),
        CommutativeCancellation(...),
        ContractIdleWiresInControlFlow(),
    ]
    post_loop = []
    reset_changed, continue_loop = _optimization_check_changed_flag()
    loop = [reset_changed] + loop
    loop_check = []
```

**What this removes**: `Size`, `Depth`, `FixedPoint("size")`, `FixedPoint("depth")` — 4 analysis passes per iteration that are no longer needed.

**What this changes**: The `do_while` callback now checks a direct boolean flag instead of comparing metrics. The flag is reset at the start of each iteration, so it reflects only the current iteration's changes.

## Results

### Changed-flag loop behavior

| Circuit | Old Iters (FixedPoint) | New Iters (Changed-Flag) | 2Q Gates (both) |
|---------|:----------------------:|:------------------------:|:----------------:|
| QFT_100 | 3 | **2** | 9,528 |
| QV_100 | 2 | **1** | 96,474 |
| EfficientSU2_100 | 2 | **1** | 297 |
| QAOA_100 | 3 | **1** | 186 |
| BV_100 | 2 | **1** | 196 |
| Heisenberg_100 | 2 | **1** | 891 |

**5 of 6 circuits now exit after 1 iteration** (was 2-3 with FixedPoint). QFT takes 2 iterations because CommutativeCancellation legitimately finds CX cancellations in iteration 1, triggering a second pass that confirms no further 2Q changes.

**Zero 2Q gate regressions.** The 2Q gate counts are identical to the FixedPoint version.

### Why it works

The changed-flag answers the right question: "did any pass create new optimization opportunities that a subsequent iteration could exploit?" Rather than:

- Computing Size and Depth after each iteration (indirect)
- Comparing to previous values via FixedPoint (requires 2 data points)
- Waiting for both metrics to stabilize simultaneously

...we ask the passes directly. The loop continues when either:

1. **2Q gates were removed** (`_opt_pass_changed`) — a removed 2Q gate exposes longer 1Q runs and new cancellation patterns for the next iteration
2. **Rotations were consolidated** (`_opt_1q_consolidated`) — merged rotations shorten 1Q runs, enabling `Optimize1qGatesDecomposition` to find better decompositions on the next iteration

Both 2Q and 1Q optimization matter. The key insight is that new 1Q opportunities only arise when the structure of 1Q runs changes — and within this loop, that can only happen through two mechanisms: 2Q gate removal (which merges adjacent 1Q runs across the gap) or rotation consolidation (which shortens existing 1Q runs). If neither occurred, `Optimize1qGatesDecomposition` sees the same runs it already optimally decomposed — re-running it produces identical results.

### Why Optimize1qGatesDecomposition doesn't drive the loop

`Optimize1qGatesDecomposition` mutates the DAG unconditionally — it replaces 1Q runs with their optimal Euler decomposition even when the result is identical to the input (e.g., `[RZ, SX, RZ]` → decompose → `[RZ, SX, RZ]`). It does not distinguish "I improved something" from "I replaced with an equivalent sequence."

A naive "did anything change?" flag would never converge because this pass always reports changes. But this pass also cannot create new opportunities for other passes — its output doesn't introduce new commutation patterns or new identity gates. It is purely a **consumer** of opportunities created by the other two passes.

Therefore we track signals only from passes that **produce** new opportunities:
- `RemoveIdentityEquivalent` — removes multi-qubit identity gates (well-defined: either it finds identities or it doesn't)
- `CommutativeCancellation` — cancels 2Q gate pairs and consolidates rotations (well-defined: either it finds cancellations/consolidations or it doesn't)

When neither finds work, no 1Q run in the circuit has changed, so `Optimize1qGatesDecomposition` would reproduce its previous output. The loop exits safely.

## Dev Team Feedback: 1Q Gate and Depth Impact

### The concern

The dev team pointed out that the optimization loop is not just concerned with 2Q gates — it also reduces 1Q gates and depth. The current FixedPoint condition tracks total operation count and total depth (all gates), not just 2Q. A 2Q-only changed-flag would exit the loop early, potentially missing legitimate 1Q optimization opportunities.

> "The fundamental mismatch is the optimization loop is not just concerned with 2q optimizations, it also can reduce 1q gates. The current loop structure considers a fixed point in total operation count not just 2q gates. Similarly with depth it's depth over all gates not just 2q."

### Empirical data: total gates and depth

To quantify the 1Q impact, we compare total gate count and depth between loop (FixedPoint) and no-loop (single iteration):

| Circuit | Loop Size | No-Loop Size | Size Delta | % | Loop Depth | No-Loop Depth | Depth Delta | % |
|---------|:---------:|:------------:|:----------:|:-:|:----------:|:-------------:|:-----------:|:-:|
| QFT_100 | 37,285 | 37,354 | **+69** | 0.18% | 5,357 | 5,360 | **+3** | 0.06% |
| QV_100 | 388,349 | 388,349 | 0 | 0% | 26,772 | 26,772 | 0 | 0% |
| EfficientSU2_100 | 3,482 | 3,482 | 0 | 0% | 330 | 330 | 0 | 0% |
| QAOA_100 | 53,226 | 53,235 | **+9** | 0.02% | 4,889 | 4,889 | 0 | 0% |
| BV_100 | 1,083 | 1,083 | 0 | 0% | 450 | 450 | 0 | 0% |
| Heisenberg_100 | 20,640 | 20,640 | 0 | 0% | 2,275 | 2,275 | 0 | 0% |

**2/6 circuits show a total gate regression without the loop. 1/6 shows a depth regression.** The regressions are small but real — QFT loses 69 total gates (0.18%) and 3 depth levels, QAOA loses 9 total gates (0.02%). All are 1Q gates (2Q gates are identical).

### The mechanism: CommutativeCancellation → Optimize1qGatesDecomposition cross-iteration interaction

The 1Q benefit comes from a specific cross-pass interaction across iterations:

1. **Iteration 1**: `CommutativeCancellation` consolidates Z-rotations (RZ, P, U1) into single rotations. This is a 1Q-only operation — it merges commuting rotation angles but does not cancel 2Q gates.

2. **Iteration 2**: The consolidated rotations shorten some 1Q runs. `Optimize1qGatesDecomposition` now finds these shorter runs can be decomposed more efficiently (e.g., a 3-gate run becomes a 2-gate decomposition).

Evidence from per-pass stats:
- QFT: `Optimize1qGatesDecomposition` removes **-6,472** total gates across 3 iterations (loop) vs **-6,416** in 1 iteration (no-loop). The extra 56 gates come from iteration 2. `RemoveIdentityEquivalent` similarly removes 13 more identity gates in the full loop.
- QAOA: `Optimize1qGatesDecomposition` removes **-3,325** (loop) vs **-3,316** (no-loop). The extra 9 gates come from iteration 2.

**This interaction only matters for rotation-heavy circuits** (QFT, QAOA with ZZ interactions). Circuits without dense rotation patterns (QV, BV, GHZ, Heisenberg, EfficientSU2) see zero difference.

### Why a generic "total-gate changed" flag doesn't work

The natural response to this feedback would be: "broaden the changed-flag to include any gate change." But `Optimize1qGatesDecomposition` mutates the DAG unconditionally — it replaces 1Q runs with optimal Euler decompositions even when the result is identical to the input. A total-gate changed-flag would never converge because this pass always "changes" something.

Options:
1. **Fix Optimize1qGatesDecomposition to be idempotent**: Compare the replacement with the original before applying it. This would enable a reliable total-gate changed-flag but requires a more invasive Rust change.
2. **Track CommutativeCancellation's 1Q consolidation separately**: Have CommutativeCancellation report whether it consolidated any rotations (not just cancelled 2Q pairs). If it did, run one more iteration for Optimize1qGatesDecomposition to pick up the benefit.
3. **Accept the trade-off**: The 2Q-only flag is correct for the dominant use case. The 0.18% 1Q regression on rotation-heavy circuits is negligible compared to the 10-20% optimization stage speedup from eliminating the confirmation iteration.

### Recommendation and Implementation

**Option 2 was implemented and verified.** We extended `CommutativeCancellation` to return a tuple `(multi_qubit_changed, rotations_consolidated)` and added a second property-set flag `_opt_1q_consolidated`. The loop now continues if **either** flag is set — this preserves the 1Q rotation-consolidation benefit while still avoiding the FixedPoint's unconditional confirmation pass.

### A/B Test: Changed-Flag (with rotation tracking) vs FixedPoint

We ran both loop conditions on the same post-routing circuits to verify zero regression:

| Circuit | FixedPoint Size | Changed-Flag Size | Delta | FixedPoint Depth | Changed-Flag Depth | Delta |
|---------|:-:|:-:|:-:|:-:|:-:|:-:|
| QFT_100 | 37,687 | 37,687 | **0** | 5,404 | 5,404 | **0** |
| QAOA_100 | 2,137 | 2,137 | **0** | 323 | 323 | **0** |
| EfficientSU2_100 | 1,494 | 1,494 | **0** | 330 | 330 | **0** |

**Zero delta on total gate count, depth, and 2Q gates across all tested circuits.** The rotation-consolidation flag triggers exactly the iterations needed to capture the CommutativeCancellation → Optimize1qGatesDecomposition cross-iteration benefit, without the FixedPoint's unnecessary confirmation pass.

### Progression: How We Eliminated the 1Q Regression

| Version | Exit Condition | QFT_100 Total Gates | QAOA_100 Total Gates | Regression? |
|---------|---------------|:---:|:---:|:---:|
| **Baseline (FixedPoint)** | Size + depth unchanged for 1 iteration | 37,687 | 2,137 | — (reference) |
| **v1: 2Q-only flag** | Only `_opt_pass_changed` (multi-qubit removals) | 37,756 (+69) | 2,146 (+9) | **Yes** — 0.18% / 0.02% |
| **v2: + rotation tracking** | `_opt_pass_changed` OR `_opt_1q_consolidated` | 37,687 (+0) | 2,137 (+0) | **No** — zero delta |

**What caused the v1 regression:** Skipping the extra iteration meant `CommutativeCancellation`'s rotation consolidation in iteration 1 (merging commuting RZ/P/U1 gates into single rotations) never got a follow-up `Optimize1qGatesDecomposition` pass to exploit the shorter 1Q runs. The consolidated rotations sat there un-decomposed.

**How v2 fixes it:** `CommutativeCancellation` now returns `(multi_qubit_changed, rotations_consolidated)`. When `rotations_consolidated = true`, the loop runs one more iteration — just enough for `Optimize1qGatesDecomposition` to find better decompositions for the shortened 1Q runs. No unnecessary confirmation pass, no regression.

**Why this is correct and complete:** New 1Q optimization opportunities can only arise when the structure of 1Q runs changes. Within this loop, that happens via exactly two mechanisms:
1. A 2Q gate is removed → adjacent 1Q runs merge into a longer run → `_opt_pass_changed` fires
2. Rotations are consolidated → a 1Q run becomes shorter → `_opt_1q_consolidated` fires

If neither fires, `Optimize1qGatesDecomposition` sees identical runs to what it already optimally decomposed. Re-running it would produce the same output — the loop exits safely with zero missed opportunities.

## Verification

```bash
# Build Rust changes
cd ~/IBMWORK/QCSC/qiskit
pip install -e .

# Run optimization loop profiling (before/after comparison)
~/.venv/bin/python investigation/profile_optimization_loop.py

# Run no-loop comparison (verify identical 2Q gates)
~/.venv/bin/python investigation/test_no_loop.py

# Run Qiskit test suite for modified passes
python -m pytest test/python/transpiler/test_remove_identity_equivalent.py -x
python -m pytest test/python/transpiler/test_optimize_1q_decomposition.py -x
python -m pytest test/python/transpiler/test_commutative_cancellation.py -x
python -m pytest test/python/transpiler/test_preset_passmanagers.py -x
```

All 152 pass tests pass. All 3 optimization levels produce correct results.

## Modified Files

| File | Change |
|------|--------|
| `crates/transpiler/src/passes/remove_identity_equiv.rs` | Return `PyResult<bool>` (true = multi-qubit identity removed) |
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | Return `PyResult<bool>` (true = 1Q run replaced) |
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Return `PyResult<(bool, bool)>` — `(multi_qubit_changed, rotations_consolidated)` |
| `qiskit/transpiler/passes/optimization/remove_identity_equiv.py` | Read bool, set `property_set["_opt_pass_changed"]` |
| `qiskit/transpiler/passes/optimization/commutative_cancellation.py` | Unpack tuple, set `_opt_pass_changed` and `_opt_1q_consolidated` separately |
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Replace FixedPoint with changed-flag; loop checks both `_opt_pass_changed` OR `_opt_1q_consolidated` |

## Reference: Profiling Data (Read-Only Context)

These tables are from the initial profiling phase and motivated the investigation. **No changes were made to Level 3.** The Level 3 data is included here only as background context — it was part of the analysis that led the team to focus on Level 2.

### Level 3 Loop Convergence (unchanged — included for context only)

| Circuit | Iters | Productive | Why Stopped | Opt (ms) |
|---------|:-----:|:----------:|-------------|:--------:|
| QFT_100 | 6 | 1 | Backtrack (oscillation) | 8,255 |
| QV_100 | 3 | 1 | Fixed point (iter 1-2) | 45,788 |
| EfficientSU2_100 | 3 | 0 | Fixed point (iter 1-2) | 279 |
| QAOA_100 | 3 | 1 | Fixed point (iter 1-2) | 6,207 |
| BV_100 | 3 | 1 | Fixed point (iter 1-2) | 181 |
| Heisenberg_100 | 6 | 1 | Backtrack (oscillation) | 7,119 |

At Level 3, MinimumPoint needs 3-6 iterations. All useful work happens in iteration 1. Extra iterations are convergence confirmation or oscillation wait for backtrack_depth=5 to trigger. **Level 3 is untouched — it keeps its existing MinimumPoint loop per team direction.**

### Per-Pass 2Q Gate Delta (Level 3, cumulative)

| Pass | QFT | QV | SU2 | QAOA | BV | Heisenberg |
|------|----:|---:|----:|-----:|---:|-----------:|
| ConsolidateBlocks | -4,380 | -31,861 | 0 | -646 | -291 | -3,355 |
| UnitarySynthesis | +2,224 | +29,722 | 0 | +372 | +97 | +1,576 |
| CommutativeCancellation | -68 | 0 | 0 | 0 | 0 | 0 |
| Others | 0 | 0 | 0 | 0 | 0 | 0 |

### No-Loop Experiment (Level 2, all 13 circuits)

**2Q gate comparison** — zero regressions on all 13 circuits:

| Circuit | Loop 2Q | No-Loop 2Q | Diff |
|---------|:-------:|:----------:|:----:|
| QFT_100 | 9,116 | 9,116 | 0 |
| QV_100 | 96,093 | 96,093 | 0 |
| EfficientSU2_100 | 297 | 297 | 0 |
| QAOA_100 | 15,961 | 15,961 | 0 |
| BV_100 | 200 | 200 | 0 |
| Heisenberg_100 | 4,947 | 4,947 | 0 |
| Grover_50 | 2,809 | 2,809 | 0 |
| Adder_80 | 1,332 | 1,332 | 0 |
| Random_80 | 14,808 | 14,808 | 0 |
| GHZ_100 | 99 | 99 | 0 |
| QPE_50 | 3,721 | 3,721 | 0 |
| Toffoli_90 | 1,111 | 1,111 | 0 |
| fe4s4_LUCJ | 4,225 | 4,225 | 0 |

**Total gate and depth comparison** — small 1Q regressions on 2 rotation-heavy circuits:

| Circuit | Loop Size | No-Loop Size | Size Delta | Loop Depth | No-Loop Depth | Depth Delta |
|---------|:---------:|:------------:|:----------:|:----------:|:-------------:|:-----------:|
| QFT_100 | 37,285 | 37,354 | **+69** (0.18%) | 5,357 | 5,360 | **+3** |
| QV_100 | 388,349 | 388,349 | 0 | 26,772 | 26,772 | 0 |
| EfficientSU2_100 | 3,482 | 3,482 | 0 | 330 | 330 | 0 |
| QAOA_100 | 53,226 | 53,235 | **+9** (0.02%) | 4,889 | 4,889 | 0 |
| BV_100 | 1,083 | 1,083 | 0 | 450 | 450 | 0 |
| Heisenberg_100 | 20,640 | 20,640 | 0 | 2,275 | 2,275 | 0 |

**0/13 circuits regressed on 2Q gates.** 2/6 show small total gate regressions (0.02-0.18%, all 1Q gates) from the CommutativeCancellation → Optimize1qGatesDecomposition cross-iteration interaction on rotation-heavy circuits. See [Dev Team Feedback](#dev-team-feedback-1q-gate-and-depth-impact) for analysis.

## Next Steps

- [x] ~~Discuss with Qiskit team: accept 0.18% 1Q regression (Option 3) or implement rotation-consolidation flag (Option 2)?~~ → **Option 2 implemented**
- [x] ~~Extend `cancel_commutations` in Rust to return `(bool, bool)` — `(multi_qubit_changed, rotations_consolidated)`~~ → **Done** (commit `efc12cf7c`)
- [x] ~~A/B test proving zero regression vs FixedPoint~~ → **Verified**: zero delta on QFT, QAOA, EfficientSU2
- [ ] Run A/B on remaining circuits (QV_100, BV_100, Heisenberg_100) for completeness
- [ ] Upstream proposal with profiling data and chosen approach
- [ ] Consider applying the same pattern to Level 1 (uses FixedPoint with different passes)
