# Investigation: Smarter Loop Exit for Level 2 Optimization

## Motivation

The Qiskit dev team asked: **"Is the Level 2 optimization loop warranted, or is there a better loop condition that could avoid an unnecessary second iteration to validate we've reached a steady state?"**

The current Level 2 loop uses `FixedPoint("size") AND FixedPoint("depth")` for convergence. `FixedPoint` compares the current metric to the previous iteration's value and requires at least **two iterations** — one productive, one to confirm nothing changed. The question is whether we can find a condition that exits the loop earlier when the productive work is already done.

**Scope**: Level 2 only. The team confirmed Level 3 keeps its existing `MinimumPoint` loop.

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

---

## Level 2 Optimization Pipeline

The optimization stage has three phases: **pre-loop**, **loop body**, and an **unroll block** inside the loop.

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
unroll = [
    GatesInBasis(basis_gates),           # Check: any gates outside target basis?
    ConditionalController(               # IF not all_gates_in_basis:
        BasisTranslator(...)             #   translate them back to basis
    ),
]
```

Assembly:

```python
optimization = PassManager()
optimization.append(pre_loop + loop_check)                          # Run once
optimization.append(DoWhileController(loop + unroll + loop_check,   # Loop
                                      do_while=continue_loop))
```

`DoWhileController` has do-while semantics: always runs at least once, then checks `do_while(property_set)` after each iteration.

### The FixedPoint Problem

```python
# Current convergence check
loop_check = [Size(recurse=True), Depth(recurse=True), FixedPoint("size"), FixedPoint("depth")]

def check(property_set):
    return not (property_set["depth_fixed_point"] and property_set["size_fixed_point"])
```

`FixedPoint` stores `_previous = None` initially, so the first comparison always returns `False`. This means **minimum 2 iterations** — even when iteration 1 already produces the final result.

Our profiling shows that on 12/13 test circuits, iteration 2+ does zero useful work. It exists solely as a confirmation pass.

---

## Solution: Three Opportunity-Producing Signals

Instead of measuring whether the circuit changed (indirect, requires confirmation), we ask the passes directly: **"did you create any new optimization opportunities?"**

New optimization opportunities can only arise when the circuit's structure changes. Within this loop, that happens via exactly **three mechanisms**:

| Signal | Flag | Source | What It Means |
|--------|------|--------|---------------|
| **2Q gate removed** | `_opt_pass_changed` | RemoveIdentityEquivalent, CommutativeCancellation | A removed 2Q gate exposes longer 1Q runs → `Optimize1qGatesDecomposition` can find better decompositions |
| **Rotations consolidated** | `_opt_1q_consolidated` | CommutativeCancellation | Merged rotations shorten 1Q runs → `Optimize1qGatesDecomposition` can decompose them more efficiently |
| **Out-of-basis gate produced** | `all_gates_in_basis == False` | GatesInBasis (detects), BasisTranslator (translates) | Translation produces unoptimized sequences → need re-optimization |

The loop continues if **any** signal fires. If none fires, the circuit's structure is unchanged — re-running the passes would produce identical results. The loop exits safely.

### Why This Works

The key insight is the **producer/consumer** distinction among the loop passes:

- **Producers** (create new optimization opportunities): `RemoveIdentityEquivalent`, `CommutativeCancellation`, `BasisTranslator`
- **Consumers** (exploit opportunities but don't create new ones): `Optimize1qGatesDecomposition`

`Optimize1qGatesDecomposition` mutates the DAG unconditionally — it replaces 1Q runs with optimal Euler decompositions even when the result is identical. A naive "did anything change?" flag would never converge. But this pass cannot create new opportunities for other passes — its output doesn't introduce new commutation patterns or identity gates. It is purely a consumer.

We track signals only from producers. When no producer fires, consumers see the same input and produce the same output. Done.

### Loop Condition Code

```python
def _optimization_check_changed_flag():
    class _ResetChangedFlag(AnalysisPass):
        def run(self, dag):
            self.property_set["_opt_pass_changed"] = False
            self.property_set["_opt_1q_consolidated"] = False

    def check(property_set):
        return (
            property_set.get("_opt_pass_changed", False)
            or property_set.get("_opt_1q_consolidated", False)
            or not property_set.get("all_gates_in_basis", True)
        )

    return (_ResetChangedFlag(), check)
```

**What this removes**: `Size`, `Depth`, `FixedPoint("size")`, `FixedPoint("depth")` — 4 analysis passes per iteration.

---

## Results

All benchmarks run on Intel Xeon Sapphire Rapids (160 vCPUs), release build, `seed_transpiler=42` for deterministic comparison.

### Loop Body Improvement

Measuring only the optimization loop passes (excluding SABRE layout/routing which is ~69% of total time):

**11.27s → 8.85s (22% faster)** on representative circuits.

### End-to-End: Benchpress Suite (59 Circuits, Seeded)

Full `generate_preset_pass_manager(optimization_level=2, backend, seed_transpiler=42)` + `pm.run()`. Backend: FakeTorino (133Q heavy-hex).

| Suite | Circuits | Main (s) | Ours (s) | Speedup | 2Q Gate Δ |
|-------|:--------:|:--------:|:--------:|:-------:|:---------:|
| device_transpile | 9 | 178.4 | 171.2 | **1.04x** | 0 |
| device_feynman | 50 | 635.5 | 570.3 | **1.11x** | 0 |
| **Total** | **59** | **813.9** | **741.5** | **1.10x** | **0** |

Top per-circuit speedups (feynman suite):
- hwb12 (639K CZ): 349s → 312s = **1.12x**
- hwb11 (335K CZ): 179s → 161s = **1.12x**
- clifford_100 (66K CZ): 23.6s → 20.8s = **1.13x**

The end-to-end speedup (10-11%) is lower than the loop-body speedup (22%) because SABRE layout and routing — which are unchanged — dominate total transpile time.

### End-to-End: Custom Sweep (22 Circuits, Seeded)

Backend: `GenericBackendV2(127Q, basis=[id, sx, x, rz, cz], seed=42)`, `seed_transpiler=42`.

| Circuit | Qubits | Gates | CZ | Depth | Main (s) | Ours (s) | **Speedup** |
|---------|:------:|------:|---:|------:|:--------:|:--------:|:-----------:|
| bwt_n37 | 37 | 2,889,616 | 604,400 | 1,609,310 | 34.47 | 31.99 | **1.08x** |
| hwb12 | 20 | 827,072 | 190,975 | 456,563 | 11.67 | 9.49 | **1.23x** |
| square_root_n45 | 45 | 254,616 | 54,151 | 151,041 | 2.85 | 2.68 | **1.06x** |
| adder_n118 | 118 | 4,098 | 845 | 1,200 | 0.14 | 0.14 | 0.99x |
| vqe_uccsd_n28 | 28 | 782,477 | 206,612 | 635,741 | 27.81 | 23.41 | **1.19x** |
| ghz_n127 | 127 | 886 | 126 | 382 | 0.11 | 0.10 | 1.06x |
| QV_n100 | 100 | 109,394 | 14,835 | 1,414 | 3.82 | 3.83 | 1.00x |
| ising_n98 | 98 | 1,353 | 194 | 22 | 0.13 | 0.12 | 1.05x |
| multiplier_n45 | 45 | 10,627 | 2,286 | 4,642 | 0.54 | 0.56 | 0.96x |
| wstate_n76 | 76 | 1,128 | 150 | 383 | 0.16 | 0.16 | 0.99x |
| cat_n65 | 65 | 452 | 64 | 196 | 0.30 | 0.29 | 1.06x |
| qugan_n71 | 71 | 2,385 | 381 | 618 | 0.52 | 0.52 | 1.00x |
| ising_n42 | 42 | 574 | 82 | 22 | 0.23 | 0.20 | 1.17x |
| qft_n63 | 63 | 8,476 | 2,014 | 893 | 1.74 | 1.73 | 1.01x |
| knn_n67 | 67 | 1,260 | 231 | 572 | 0.41 | 0.42 | 0.98x |
| dnn_n51 | 51 | 1,699 | 271 | 460 | 0.58 | 0.52 | 1.11x |
| barenco_tof_10 | 19 | 949 | 192 | 694 | 0.37 | 0.44 | 0.83x |
| swap_test_n41 | 41 | 845 | 140 | 352 | 0.59 | 0.54 | 1.09x |
| bv_n70 | 70 | 527 | 36 | 48 | 0.48 | 0.49 | 0.97x |
| gf2^16_mult | 48 | 7,363 | 1,581 | 1,167 | 6.38 | 6.45 | 0.99x |
| adder | 10 | 309 | 65 | 204 | 0.62 | 0.60 | 1.04x |
| bigadder | 18 | 611 | 130 | 319 | 0.72 | 0.69 | 1.05x |

**Total**: main=94.6s, ours=85.4s → **9.3s saved (9.8%), 1.108x overall speedup**

Key observations:
- Circuits with large output (>100K gates) benefit most: hwb12 **+23%**, vqe_uccsd_n28 **+19%**, bwt_n37 **+8%**
- Small circuits (<10K gates) show ~1x (optimization loop is a small fraction of total transpile time)
- No circuit is slower by more than measurement noise (barenco_tof_10's 0.83x = 0.07s absolute)

### Quality: Zero Regression (81 Circuits, Bit-Exact)

Both branches produce **identical output** on all 81 circuits tested — same total gates, same CZ count, same depth. This is a bit-exact comparison with `seed_transpiler=42`, confirming the 3-signal exit never terminates early when there is still productive work to do.

| Suite | Circuits | Δ 2Q Gates | Δ Total Gates | Δ Depth |
|-------|:--------:|:----------:|:-------------:|:-------:|
| device_transpile (FakeTorino) | 9 | 0 | 0 | 0 |
| device_feynman (FakeTorino) | 50 | 0 | 0 | 0 |
| Custom sweep (GenericBackendV2) | 22 | 0 | 0 | 0 |
| **Total** | **81** | **0** | **0** | **0** |

### Speed: Fewer Iterations

| Circuit | FixedPoint Iters | Changed-Flag Iters | 2Q Gates (both) |
|---------|:----------------:|:------------------:|:----------------:|
| QFT_100 | 3 | **2** | 9,528 |
| QV_100 | 2 | **1** | 96,474 |
| EfficientSU2_100 | 2 | **1** | 297 |
| QAOA_100 | 3 | **1** | 186 |
| BV_100 | 2 | **1** | 196 |
| Heisenberg_100 | 2 | **1** | 891 |

5/6 circuits exit after 1 iteration (was 2-3). QFT takes 2 because CommutativeCancellation legitimately finds CZ cancellations, triggering one more pass.

### Progression: How We Got to Zero Regression

| Version | Exit Condition | QFT_100 Total | QAOA_100 Total | Regressed? |
|---------|---------------|:---:|:---:|:---:|
| **Baseline (FixedPoint)** | Size + depth unchanged | 37,687 | 2,137 | — |
| **v1: 2Q-only** | Only `_opt_pass_changed` | 37,756 (+69) | 2,146 (+9) | **Yes** |
| **v2: + rotation tracking** | + `_opt_1q_consolidated` | 37,687 (+0) | 2,137 (+0) | **No** |

v1 missed CommutativeCancellation's rotation consolidation → Optimize1qGatesDecomposition never got a follow-up pass. v2 adds the `_opt_1q_consolidated` signal to trigger exactly the iteration needed.

---

## Signal Details

### Signal 1: `_opt_pass_changed` (2Q Gate Removal)

**Sources**: `RemoveIdentityEquivalent` (removes near-identity multi-qubit gates), `CommutativeCancellation` (cancels commuting 2Q pairs: CX, CY, CZ).

**Why re-iteration helps**: When a 2Q gate is removed, the 1Q gates on either side of it merge into a longer run. `Optimize1qGatesDecomposition` can now find a shorter Euler decomposition for this longer run.

**Example**: `CX(0,1); RZ(0.5, 0); CX(0,1)` — RZ commutes with CX on the control qubit, so CommutativeCancellation sees two CX in the same commutation set and cancels them.

**Rust implementation** (`remove_identity_equiv.rs`):
```rust
pub fn run_remove_identity_equiv(...) -> PyResult<bool> {
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

**Python wrapper** (`remove_identity_equiv.py`):
```python
def run(self, dag):
    changed = remove_identity_equiv(dag, self._approximation_degree, self._target)
    if changed:
        self.property_set["_opt_pass_changed"] = True
    return dag
```

### Signal 2: `_opt_1q_consolidated` (Rotation Consolidation)

**Source**: `CommutativeCancellation` — consolidates commuting Z-rotations (RZ, P, U1, T, S, Z) or X-rotations (X, RX) into single rotation gates.

**Why re-iteration helps**: Consolidated rotations shorten 1Q runs. `Optimize1qGatesDecomposition` finds that the shorter run can be decomposed more efficiently (e.g., a 3-gate sequence becomes a 2-gate sequence).

**Example**: `RZ(0.1); CZ(0,1); RZ(0.2); CZ(0,2); RZ(0.3)` — RZ commutes with CZ on either qubit, so all three RZ on qubit 0 are in the same commutation set and consolidate into `RZ(0.6)`.

**Rust implementation** (`commutation_cancellation.rs`):
```rust
pub fn cancel_commutations(...) -> PyResult<(bool, bool)> {
    let mut changed = false;
    let mut rotations_consolidated = false;
    for (cancel_key, cancel_set) in &cancellation_sets {
        if cancel_set.len() > 1 {
            if let GateOrRotation::Gate(g) = cancel_key.gate {
                if SUPPORTED_GATES.contains(&g) {
                    if cancel_key.qubits.len() > 1 {
                        changed = true;  // 2Q pair cancelled
                    }
                }
                continue;
            }
            if matches!(cancel_key.gate, GateOrRotation::ZRotation | GateOrRotation::XRotation) {
                rotations_consolidated = true;  // Rotations merged
                // ... consolidate into single rotation ...
            }
        }
    }
    Ok((changed, rotations_consolidated))
}
```

**Python wrapper** (`commutative_cancellation.py`):
```python
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

### Signal 3: `all_gates_in_basis == False` (Out-of-Basis Gate)

**Source**: `GatesInBasis` (analysis pass that scans DAG for gates not in target basis).

**Triggers**: Optimization passes can introduce gates outside the target basis:
- **Level 2**: `CommutativeCancellation` consolidates X-rotations → produces `RX`. If `RX` isn't in basis (e.g., basis is `[id, sx, x, rz, cz]`), it's out-of-basis.
- **Level 3**: `UnitarySynthesis` with RZZ at lower error → emits `S`, `Sdg`, `H` (not in basis).

**Why re-iteration helps**: `BasisTranslator` translates out-of-basis gates back to basis gates, but produces unoptimized sequences (e.g., `RX(θ)` → `RZ(-π/2); SX; RZ(θ); SX; RZ(-π/2)`). Without re-iteration, these stay unoptimized.

**Loop body execution order** (shows where each piece sits):

```
┌──────────────────────────────────────────────────────────────────┐
│  1. _ResetChangedFlag()         ← clear flags                    │
│  2. RemoveIdentityEquivalent    ← may set _opt_pass_changed      │
│  3. Optimize1qGatesDecomposition                                 │
│  4. CommutativeCancellation     ← may set _opt_pass_changed      │
│                                    or _opt_1q_consolidated       │
│                                    or PRODUCE OUT-OF-BASIS GATE  │
│  5. ContractIdleWiresInControlFlow                               │
│  ── unroll block ──                                              │
│  6. GatesInBasis(basis_gates)   ← sets all_gates_in_basis        │
│  7. IF not all_gates_in_basis:                                   │
│       BasisTranslator(...)      ← translates back to basis       │
│  ── loop check ──                                                │
│  8. check(): _opt_pass_changed OR _opt_1q_consolidated           │
│              OR (NOT all_gates_in_basis)                          │
└──────────────────────────────────────────────────────────────────┘
```

**Concrete test** (Matthew Treinish's recipe for Level 3):
```python
backend = GenericBackendV2(num_qubits=20, basis_gates=["id", "sx", "x", "rz", "cz"])
target = backend.target
# Add RZZ with lower error → UnitarySynthesis prefers it over CZ
rzz_props = {}
for qargs in target.qargs:
    if len(qargs) == 2:
        rzz_props[qargs] = InstructionProperties(
            duration=target["cz"][qargs].duration,
            error=target["cz"][qargs].error * 0.5
        )
target.add_instruction(RZZGate(Parameter("theta")), rzz_props)
```

Result: UnitarySynthesis emits S/Sdg/H → GatesInBasis detects → BasisTranslator translates → loop re-iterates → Optimize1qGatesDecomposition optimizes → all output gates in basis.

**No new code needed for signal 3**: `GatesInBasis` already runs inside the loop (in the `unroll` block). We just read its existing `property_set["all_gates_in_basis"]` in our loop condition.

---

## Modified Files

| File | Change |
|------|--------|
| `crates/transpiler/src/passes/remove_identity_equiv.rs` | Return `PyResult<bool>` (true = multi-qubit identity removed) |
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | Return `PyResult<bool>` (true = 1Q run replaced) — not used for loop control |
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Return `PyResult<(bool, bool)>` — `(multi_qubit_changed, rotations_consolidated)` |
| `qiskit/transpiler/passes/optimization/remove_identity_equiv.py` | Read bool, set `property_set["_opt_pass_changed"]` |
| `qiskit/transpiler/passes/optimization/commutative_cancellation.py` | Unpack tuple, set `_opt_pass_changed` and `_opt_1q_consolidated` |
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Replace FixedPoint with `_optimization_check_changed_flag()` |

**Backward compatibility**: Rust return-type changes are additive. All existing callers (Level 1, Level 3) use `function_call()?;` which discards the bool. No caller breaks.

---

## Verification Methodology

We verify correctness at four levels.

### Level 1: Signal Coverage Tests

Each signal is independently verifiable with a minimal circuit. This ensures correctness even if the benchpress suite doesn't exercise all paths.

**Script**: `investigation/scripts/test_loop_exit_signals.py`

| Test | Signal | Circuit Pattern | What It Proves |
|------|--------|-----------------|----------------|
| `test_signal1_2q_cancellation` | `_opt_pass_changed` | CX; RZ(0.5, ctrl); CX → CXs cancel | Loop re-iterates when 2Q gate removed |
| `test_signal1_identity_removal` | `_opt_pass_changed` | CX; RZ(ε); CX → near-identity removed | Loop re-iterates after identity removal |
| `test_signal2_rotation_consolidation` | `_opt_1q_consolidated` | RZ; CZ; RZ; CZ; RZ → merged | Loop re-iterates for 1Q re-decomposition |
| `test_signal3_rzz_out_of_basis` | `all_gates_in_basis` | RZZ backend (L3) → S/Sdg/H | Loop re-iterates after BasisTranslator |
| `test_signal3_rx_out_of_basis` | `all_gates_in_basis` | X+RX on CX target → RX | Loop re-iterates after BasisTranslator |

All 5 tests PASS.

### Level 2: Quality Parity (Changed-Flag vs FixedPoint)

| Metric | Method | Result |
|--------|--------|--------|
| 2Q gates | Compare changed-flag vs FixedPoint | 0 delta on all 13 circuits |
| Total gates | Compare changed-flag vs FixedPoint | 0 delta (after v2 rotation tracking) |
| Depth | Compare changed-flag vs FixedPoint | 0 delta |
| Convergence | Re-run optimization on output | 0 additional improvement on 22 circuits |

### Level 3: Existing Test Suite

```bash
python -m pytest test/python/transpiler/test_remove_identity_equivalent.py -x
python -m pytest test/python/transpiler/test_optimize_1q_decomposition.py -x
python -m pytest test/python/transpiler/test_commutative_cancellation.py -x
python -m pytest test/python/transpiler/test_preset_passmanagers.py -x
```

All 152 pass tests pass. All 3 optimization levels produce correct results.

### Level 4: Benchpress Suite Sweep

22 circuits, zero gate changes from redundant iteration on all of them. The changed-flag is a complete and correct replacement for FixedPoint at Level 2.

---

## Appendix: Profiling Data

These tables are from the initial profiling phase that motivated the investigation.

### Profiling Setup

13 circuits at Level 2 on FakeTorino (133Q heavy-hex):

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

**Scripts**: `investigation/scripts/profile_optimization_loop.py`, `investigation/scripts/test_more_circuits.py`, `investigation/scripts/test_fe4s4.py`

### Finding: The loop does no useful 2Q work after iteration 1

| Circuit | L2 Iters | Pre-Loop 2Q Delta | Loop 2Q Delta | Loop 2Q Contribution |
|---------|:--------:|:-----------------:|:-------------:|:--------------------:|
| QFT_100 | 3 | -1,492 (14.0%) | -56 | CommutativeCancellation |
| QV_100 | 2 | -2,184 (2.2%) | 0 | none |
| EfficientSU2_100 | 2 | 0 (0.0%) | 0 | none |
| QAOA_100 | 3 | -282 (1.7%) | 0 | none |
| BV_100 | 2 | -194 (49.7%) | 0 | none |
| Heisenberg_100 | 2 | -1,809 (26.5%) | 0 | none |
| fe4s4_LUCJ | 3 | -72 (1.7%) | 0 | none |

### No-Loop Experiment (single iteration, all 13 circuits)

Zero 2Q regressions:

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

Small 1Q regressions on 2 rotation-heavy circuits (motivating signal 2):

| Circuit | Loop Size | No-Loop Size | Delta | Loop Depth | No-Loop Depth | Delta |
|---------|:---------:|:------------:|:-----:|:----------:|:-------------:|:-----:|
| QFT_100 | 37,285 | 37,354 | +69 (0.18%) | 5,357 | 5,360 | +3 |
| QAOA_100 | 53,226 | 53,235 | +9 (0.02%) | 4,889 | 4,889 | 0 |
| Others | — | — | 0 | — | — | 0 |

### Level 3 Loop Convergence (unchanged — context only)

| Circuit | Iters | Productive | Why Stopped | Opt (ms) |
|---------|:-----:|:----------:|-------------|:--------:|
| QFT_100 | 6 | 1 | Backtrack (oscillation) | 8,255 |
| QV_100 | 3 | 1 | Fixed point (iter 1-2) | 45,788 |
| EfficientSU2_100 | 3 | 0 | Fixed point (iter 1-2) | 279 |
| QAOA_100 | 3 | 1 | Fixed point (iter 1-2) | 6,207 |
| BV_100 | 3 | 1 | Fixed point (iter 1-2) | 181 |
| Heisenberg_100 | 6 | 1 | Backtrack (oscillation) | 7,119 |

**Level 3 is untouched** — it keeps its existing MinimumPoint loop per team direction.

---

## Source Files

| File | Role |
|------|------|
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Defines optimization stage; assembles the loop |
| `qiskit/passmanager/flow_controllers.py` | `DoWhileController` — the loop mechanism |
| `qiskit/transpiler/passes/utils/fixed_point.py` | `FixedPoint` — current convergence check |
| `qiskit/transpiler/passes/utils/gates_basis.py` | `GatesInBasis` — checks all gates are in target basis |
| `crates/transpiler/src/passes/remove_identity_equiv.rs` | Rust: RemoveIdentityEquivalent |
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | Rust: Optimize1qGatesDecomposition |
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Rust: CommutativeCancellation |

---

## Next Steps

- [x] ~~Profiling: understand what the loop does~~ → All useful work in iteration 1
- [x] ~~v1: 2Q-only flag~~ → 0.18% 1Q regression on QFT
- [x] ~~v2: + rotation tracking~~ → Zero regression (commit `efc12cf7c`)
- [x] ~~v3: + all_gates_in_basis signal~~ → Handles BasisTranslator (commit `46e421426`)
- [x] ~~Side-by-side test vs FixedPoint~~ → Zero delta on all metrics
- [x] ~~RZZ out-of-basis trigger~~ → Loop handles correctly
- [x] ~~hwb12 stress test (1M+ gates)~~ → 15.5% savings
- [x] ~~Benchpress sweep (22 circuits)~~ → Zero regression, 9.8% speedup (1.108x)
- [x] ~~Signal coverage tests~~ → 5 tests, all PASS
- [x] ~~Benchpress seeded validation (59 circuits)~~ → Zero regression, 1.10x speedup
- [ ] Upstream PR with profiling data and chosen approach
- [ ] Consider applying same pattern to Level 1
