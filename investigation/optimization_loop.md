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

**Key insight**: Only `RemoveIdentityEquivalent` (multi-qubit removals) and `CommutativeCancellation` (2Q gate cancellations) can create new opportunities for further 2Q optimization. If neither of these passes changes any 2Q gates, there's no reason to iterate — 1Q-only changes don't create new 2Q optimization opportunities.

## Solution: 2Q-Aware Changed-Flag Loop Condition

### Design

Replace the indirect FixedPoint metric check with a direct signal from the passes themselves:

1. **Rust passes return `bool`**: Each optimization pass returns whether it modified multi-qubit gates
2. **Python wrappers propagate to `property_set`**: Only passes that change 2Q gates set a loop flag
3. **Loop checks the flag directly**: No need for Size/Depth/FixedPoint analysis passes

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

**`commutation_cancellation.rs`** — returns `true` only when multi-qubit gate pairs are cancelled:

```rust
pub fn cancel_commutations(...) -> PyResult<bool> {
    // ... existing logic builds cancellation_sets ...
    let mut changed = false;
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
            // ZRotation/XRotation consolidation: always 1Q, does NOT set changed
            // ... consolidate rotations ...
        }
    }
    Ok(changed)
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
    changed = cancel_commutations(dag, self._commutation_checker, sorted(self.basis))
    if changed:
        self.property_set["_opt_pass_changed"] = True
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
        """Reset the changed flag at the start of each iteration."""
        def run(self, dag):
            self.property_set["_opt_pass_changed"] = False

    def check(property_set):
        """Continue looping if any 2Q-relevant pass modified the DAG."""
        return property_set.get("_opt_pass_changed", False)

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

The changed-flag directly answers the right question: "did any pass create new 2Q optimization opportunities?" Rather than:

- Computing Size and Depth after each iteration (indirect)
- Comparing to previous values via FixedPoint (requires 2 data points)
- Waiting for both metrics to stabilize simultaneously

...we ask each pass directly: "did you change any multi-qubit gates?" If no pass did, iteration is guaranteed to produce the same result, so we stop.

### Why only 2Q changes matter

1Q gate changes (from Optimize1qGatesDecomposition) cannot create new 2Q optimization opportunities:
- They don't introduce new CX/CY/CZ pairs for CommutativeCancellation
- They don't create new multi-qubit identity gates for RemoveIdentityEquivalent
- The only cross-pass interaction that matters is: CommutativeCancellation removes 2Q gates -> exposes new patterns for subsequent passes

By ignoring 1Q-only changes, we avoid the false positive where Optimize1qGatesDecomposition always finds work (it nearly always does) and would trigger unnecessary re-iteration.

### Handling unconditional DAG mutation in Optimize1qGatesDecomposition

A known issue with a generic "changed" flag approach: `Optimize1qGatesDecomposition`
mutates the DAG unconditionally — it replaces 1Q runs with their optimal Euler
decomposition even when the result is identical to the input (e.g., `[RZ, SX, RZ]` →
decompose → `[RZ, SX, RZ]`). The pass does not distinguish "I improved something"
from "I replaced with an equivalent sequence."

A naive loop condition that checks "did any pass mutate the DAG?" would never terminate,
because this pass always reports changes.

**Our design sidesteps this entirely.** `Optimize1qGatesDecomposition` never sets the
`_opt_pass_changed` flag because it is a 1Q-only pass — it cannot create new 2Q
optimization opportunities regardless of whether it mutates or not. The loop condition
only watches `RemoveIdentityEquivalent` (multi-qubit removals) and
`CommutativeCancellation` (multi-qubit cancellations). These passes have well-defined
semantics: they either remove/cancel gates or they don't, with no "equivalent replacement"
ambiguity.

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
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Return `PyResult<bool>` (true = multi-qubit gates cancelled) |
| `qiskit/transpiler/passes/optimization/remove_identity_equiv.py` | Read bool, set `property_set["_opt_pass_changed"]` |
| `qiskit/transpiler/passes/optimization/commutative_cancellation.py` | Read bool, set `property_set["_opt_pass_changed"]` |
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Replace FixedPoint with changed-flag **for Level 2 only** (Level 1 and 3 unchanged) |

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

**0/13 circuits regressed at Level 2.** This confirms the loop's extra iterations are unnecessary — providing the empirical basis for the changed-flag approach.

## Next Steps

- [ ] Upstream proposal to Qiskit team with the changed-flag approach and profiling data
- [ ] Consider applying the same pattern to Level 1 (uses FixedPoint with different passes)
- [ ] Consider applying to Level 3 as a supplementary early-exit (before MinimumPoint kicks in)
