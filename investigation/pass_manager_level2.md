# Investigation: Qiskit Level 2 Pass Manager Construction

## Overview

This document analyzes the Qiskit transpiler pass manager at `optimization_level=2`, focusing on the optimization stage loop: what it does, why it exists, whether it's necessary, and opportunities for improvement.

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

## Source Files

| File | Role |
|------|------|
| `qiskit/transpiler/preset_passmanagers/level2.py` | Level 2 entry point — assembles the StagedPassManager |
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Defines each stage as a plugin class (init, layout, routing, translation, optimization, scheduling) |
| `qiskit/transpiler/preset_passmanagers/common.py` | Shared helpers: `generate_unroll_3q`, `generate_translation_passmanager`, `get_vf2_limits` |
| `qiskit/passmanager/flow_controllers.py` | `DoWhileController` — the loop mechanism |
| `qiskit/transpiler/passes/utils/fixed_point.py` | `FixedPoint` — convergence check (used at level 1 and 2) |
| `qiskit/transpiler/passes/utils/minimum_point.py` | `MinimumPoint` — local minimum tracker (used at level 3) |

## Full Pipeline at Level 2

The `StagedPassManager` runs 6 stages in order. Each stage is built by a plugin class in `builtin_plugins.py`.

### Stage 1: Init (lines 135-177 of builtin_plugins.py)

Prepares the circuit before layout/routing. At level 2:

```
UnitarySynthesis(min_qubits=3)       # Synthesize 3+ qubit unitaries into basis gates
HighLevelSynthesis(min_qubits=3)     # Decompose high-level objects (e.g., MCX, Permutation)
Unroll3qOrMore                       # Break remaining 3+ qubit gates into 1Q/2Q
ElidePermutations                    # Remove permutation gates (if routing enabled)
RemoveDiagonalGatesBeforeMeasure     # Drop diagonal gates immediately before measurement
RemoveIdentityEquivalent             # Remove gates equivalent to identity
InverseCancellation                  # Cancel adjacent inverse gate pairs (H·H, S·Sdg, etc.)
ContractIdleWiresInControlFlow       # Remove unused wires in control-flow blocks
CommutativeCancellation              # Cancel gates that commute and are inverses
ConsolidateBlocks                    # Collect 2Q blocks → multiply into 4×4 unitary
Split2QUnitaries                     # Split product-state 2Q unitaries back to 1Q gates
```

**Purpose**: Clean up the circuit so layout/routing sees a simplified DAG. ConsolidateBlocks + Split2QUnitaries is key — it compresses 2Q gate sequences into optimal unitaries, then splits out any that are actually separable (product states).

### Stage 2: Layout

```
VF2Layout(call_limit=(5_000_000, 10_000))   # Find perfect layout via subgraph isomorphism
  └─ fallback: SabreLayout(max_iterations=2, swap_trials=20, layout_trials=20)
```

### Stage 3: Routing

```
CheckMap                             # Is routing needed?
  └─ if needed: SabreSwap(heuristic="decay", trials=20)
VF2PostLayout                        # Try to find a better layout post-routing
```

### Stage 4: Translation

```
UnitarySynthesis                     # Synthesize unitaries into basis gates
HighLevelSynthesis                   # Decompose high-level objects
BasisTranslator                      # Translate remaining non-basis gates via equivalence library
```

### Stage 5: Optimization (lines 505-605 of builtin_plugins.py)

This is the focus of this investigation.

### Stage 6: Scheduling

```
TimeUnitConversion → ALAPScheduleAnalysis → PadDelay
```

## The Optimization Stage in Detail

The optimization stage has three phases: **pre-loop**, **loop**, and **post-loop**.

```python
# builtin_plugins.py, lines 505-533
case 2:
    pre_loop = [
        ConsolidateBlocks(...),
        UnitarySynthesis(...),
    ]
    loop = [
        RemoveIdentityEquivalent(...),
        Optimize1qGatesDecomposition(...),
        CommutativeCancellation(...),
        ContractIdleWiresInControlFlow(),
    ]
    post_loop = []
    loop_check, continue_loop = _optimization_check_fixed_point()
```

Assembled as:

```python
# lines 601-604
optimization = PassManager()
optimization.append(pre_loop + loop_check)                              # Run once
optimization.append(DoWhileController(loop + unroll + loop_check,       # Loop
                                      do_while=continue_loop))
optimization.append(post_loop)                                          # Run once (empty at L2)
```

### Pre-Loop (runs once)

| Pass | What it does |
|------|-------------|
| **ConsolidateBlocks** | Collects adjacent 1Q+2Q gates on the same qubits into blocks, multiplies each block into a unitary matrix, and resynthesizes using KAK/Weyl decomposition (optimal 0-3 CX gates). This is the main gate reduction pass. |
| **UnitarySynthesis** | Handles any remaining `UnitaryGate` nodes (including >2Q unitaries from ConsolidateBlocks) by decomposing them into basis gates. |

**Why pre-loop?** After routing inserts SWAPs, there are new sequences of 2Q gates that can be consolidated. This is the first time the post-routing circuit gets block optimization. It runs once before the loop because it's expensive (block collection + matrix multiplication + KAK decomposition for each block) and the loop passes build on its output.

### Loop Body (repeats until convergence)

| Pass | What it does |
|------|-------------|
| **RemoveIdentityEquivalent** | Removes gates that are equivalent to identity (within approximation tolerance). After ConsolidateBlocks resynthesizes blocks, some gates may become identity. |
| **Optimize1qGatesDecomposition** | Merges consecutive 1Q gates into a single rotation and decomposes into the optimal basis sequence (e.g., RZ-SX-RZ). A chain of 5 rotations → 1-2 basis gates. |
| **CommutativeCancellation** | Finds pairs of gates that commute with everything between them and cancel (e.g., two CX gates on the same qubits with only commuting 1Q gates between them). |
| **ContractIdleWiresInControlFlow** | Removes unused qubit wires from control-flow blocks (`if_else`, `while_loop`, etc.). |
| **GatesInBasis** (conditional) | Checks if all gates are in the target basis. If not, runs BasisTranslator to fix. This handles cases where optimization passes introduce non-basis gates. |

**Why loop?** Each pass can create new opportunities for the next:
- CommutativeCancellation removes a gate → exposes adjacent 1Q gates for Optimize1qGatesDecomposition
- Optimize1qGatesDecomposition merges 1Q chains → may produce identity gates for RemoveIdentityEquivalent
- RemoveIdentityEquivalent removes a gate → enables new CommutativeCancellation patterns

### Convergence Criterion: FixedPoint

```python
# builtin_plugins.py, lines 468-473
def _optimization_check_fixed_point():
    def check(property_set):
        return not (property_set["depth_fixed_point"] and property_set["size_fixed_point"])

    setup = [Size(recurse=True), Depth(recurse=True), FixedPoint("size"), FixedPoint("depth")]
    return (setup, check)
```

At the end of each iteration:
1. `Size` counts total gates → stores in `property_set["size"]`
2. `Depth` computes circuit depth → stores in `property_set["depth"]`
3. `FixedPoint("size")` compares current size to previous iteration's size. If equal → `size_fixed_point = True`
4. `FixedPoint("depth")` same for depth

**Loop stops when**: both `size_fixed_point` AND `depth_fixed_point` are `True` — meaning neither gate count nor depth changed from the previous iteration.

**Max iterations**: 1000 (DoWhileController default). In practice, convergence happens much sooner.

### Comparison: Level 3 Uses MinimumPoint Instead

Level 3 uses `MinimumPoint(["depth", "size"], prefix, backtrack_depth=5)`:

- Tracks the **best (lowest) score** seen across iterations as a `(depth, size)` tuple
- If score improves → update the stored minimum, reset counter
- If score worsens → increment counter. After `backtrack_depth=5` consecutive non-improvements → **restore the best DAG and stop**
- If score equals the stored minimum → fixed point reached, stop

This is more robust than FixedPoint because synthesis can be non-deterministic (e.g., two equivalent decompositions with same gate count but different structure). FixedPoint requires exact equality; MinimumPoint tolerates oscillation and backtracks to the best result.

Another key difference: Level 3 puts ConsolidateBlocks + UnitarySynthesis **inside** the loop (not pre-loop), so reconsolidation happens every iteration. More expensive but catches more optimization opportunities.

## What Each Pass Does (Detailed)

### ConsolidateBlocks

**Source**: `qiskit/transpiler/passes/optimization/consolidate_blocks.py` + `crates/transpiler/src/passes/consolidate_blocks.rs`

1. Calls `dag.collect_2q_runs()` — Rust bicolor DAG algorithm that finds maximal runs of 1Q+2Q gates acting on ≤2 qubits
2. Also calls `dag.collect_1q_runs()` for single-qubit optimization
3. For each 2Q block:
   - Multiplies gate matrices → 4×4 unitary
   - Uses `TwoQubitBasisDecomposer` (KAK/Weyl) to compute optimal decomposition
   - If decomposed gate count < original block size → replace the block
4. For >2Q blocks: wraps in a generic `UnitaryGate` (no efficient decomposer exists)

### Optimize1qGatesDecomposition

Finds consecutive runs of 1Q gates on the same qubit, multiplies them into a single 2×2 unitary, and decomposes into the minimal basis sequence (e.g., a single RZ-SX-RZ instead of RZ-RZ-SX-RZ-SX).

### CommutativeCancellation

Uses a commutation checker to identify gates that can be reordered. When two self-inverse gates (like CX) commute with all gates between them, they cancel. When two rotation gates commute, their angles can be combined.

### RemoveIdentityEquivalent

Removes any gate whose unitary is close to identity (within `approximation_degree` tolerance). This catches near-identity gates produced by synthesis that have negligible effect.

## Key Questions for Profiling

1. **How many loop iterations** do typical benchpress circuits need? If 1-2 for most circuits, the loop mechanism is overhead with little benefit.

2. **Is the pre-loop ConsolidateBlocks redundant** with the init-stage ConsolidateBlocks? The init version runs before routing; the pre-loop version runs after. Post-routing consolidation catches SWAP-adjacent gate sequences, so it's likely not redundant — but how much does it actually reduce?

3. **Does the conditional BasisTranslator ever fire** inside the loop? If optimization passes never introduce non-basis gates, this check is wasted every iteration.

4. **Would MinimumPoint be better than FixedPoint** at level 2? Level 3 uses it for good reason — synthesis non-determinism can prevent exact fixed-point convergence.

5. **Per-pass timing**: Which pass in the loop is the bottleneck? CommutativeCancellation does commutativity analysis which can be expensive on large circuits.

## Level 3 Optimization Loop Profiling Results

**Setup**: 6 circuits (100Q each) on FakeTorino (133Q heavy-hex), optimization_level=3. Instrumented the DoWhileController loop pass-by-pass: timing, gate counts, 2Q deltas, MinimumPoint convergence.

**Script**: `investigation/profile_optimization_loop.py`

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

### Circuits Tested

| Circuit | Description | Input Gates |
|---------|-------------|:-----------:|
| QFT_100 | 100-qubit QFT — chain topology | ~5K |
| QV_100 | 100-qubit Quantum Volume — dense random | ~100K |
| EfficientSU2_100 | 100-qubit EfficientSU2, linear entanglement | ~700 |
| QAOA_100 | 100-qubit QAOA (3 layers, random ZZ) | ~1.2K |
| BV_100 | 100-qubit Bernstein-Vazirani | ~300 |
| Heisenberg_100 | 100-qubit 10×10 square Heisenberg (3 Trotter steps) | ~3.2K |

### Table 1: Loop Convergence & Timing

| Circuit | Iters | Productive | Why Stopped | Opt (ms) | Pre-Opt (ms) | Opt % |
|---------|:-----:|:----------:|-------------|:--------:|:------------:|:-----:|
| QFT_100 | 6 | 1 | Backtrack (oscillation) | 8,255 | 4,679 | 63.8% |
| QV_100 | 3 | 1 | Fixed point (iter 1-2) | 45,788 | 84,394 | 35.2% |
| EfficientSU2_100 | 3 | 0 | Fixed point (iter 1-2) | 279 | 2,323 | 10.7% |
| QAOA_100 | 3 | 1 | Fixed point (iter 1-2) | 6,207 | 10,334 | 37.5% |
| BV_100 | 3 | 1 | Fixed point (iter 1-2) | 181 | 537,895 | 0.0% |
| Heisenberg_100 | 6 | 1 | Backtrack (oscillation) | 7,119 | 2,552 | 73.6% |

**Productive** = iterations that actually changed 2Q gate count. All useful work happens in iteration 1. Extra iterations are convergence confirmation or oscillation wait for MinimumPoint to trigger.

### Table 2: Gate Quality (2Q Gates)

| Circuit | Pre-Opt | After Iter 1 | Final | Iter 1 Reduction | Loop Extra | Total Reduction |
|---------|:-------:|:------------:|:-----:|:----------------:|:----------:|:---------------:|
| QFT_100 | 11,029 | 8,821 | 8,805 | 2,208 (20.0%) | 16 | 2,224 (20.2%) |
| QV_100 | 98,292 | 96,153 | 96,153 | 2,139 (2.2%) | 0 | 2,139 (2.2%) |
| EfficientSU2_100 | 297 | 297 | 297 | 0 (0.0%) | 0 | 0 (0.0%) |
| QAOA_100 | 16,500 | 16,226 | 16,226 | 274 (1.7%) | 0 | 274 (1.7%) |
| BV_100 | 390 | 196 | 196 | 194 (49.7%) | 0 | 194 (49.7%) |
| Heisenberg_100 | 6,489 | 4,710 | 4,710 | 1,779 (27.4%) | 0 | 1,779 (27.4%) |

**Loop Extra** = additional 2Q gate reduction from iterations 2+. Only QFT gets 16 extra gates (0.2% of total reduction) from the loop — all other circuits get **zero** benefit from iterating.

### Table 3: Per-Pass Time (ms, cumulative across all iterations)

| Pass | QFT | QV | SU2 | QAOA | BV | Heisenberg |
|------|----:|---:|----:|-----:|---:|-----------:|
| ConsolidateBlocks | 5,293 | 26,708 | 192 | 4,329 | 93 | 5,356 |
| UnitarySynthesis | 1,265 | 9,588 | 0 | 748 | 50 | 813 |
| Optimize1qGatesDecomposition | 808 | 4,631 | 41 | 564 | 19 | 458 |
| CommutativeCancellation | 847 | 4,833 | 42 | 561 | 18 | 461 |
| RemoveIdentityEquivalent | 42 | 28 | 3 | 5 | 1 | 30 |
| ContractIdleWiresInControlFlow | 0 | 0 | 0 | 0 | 0 | 0 |

### Table 4: Per-Pass Time (% of optimization stage)

| Pass | QFT | QV | SU2 | QAOA | BV | Heisenberg |
|------|----:|---:|----:|-----:|---:|-----------:|
| ConsolidateBlocks | 64.1% | 58.3% | 68.8% | 69.7% | 51.3% | 75.2% |
| UnitarySynthesis | 15.3% | 20.9% | 0.0% | 12.1% | 27.6% | 11.4% |
| Optimize1qGatesDecomposition | 9.8% | 10.1% | 14.6% | 9.1% | 10.6% | 6.4% |
| CommutativeCancellation | 10.3% | 10.6% | 15.2% | 9.0% | 10.0% | 6.5% |
| RemoveIdentityEquivalent | 0.5% | 0.1% | 1.2% | 0.1% | 0.6% | 0.4% |
| ContractIdleWiresInControlFlow | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |

### Table 5: Per-Pass 2Q Gate Delta (cumulative across all iterations)

| Pass | QFT | QV | SU2 | QAOA | BV | Heisenberg |
|------|----:|---:|----:|-----:|---:|-----------:|
| ConsolidateBlocks | -4,380 | -31,861 | 0 | -646 | -291 | -3,355 |
| UnitarySynthesis | +2,224 | +29,722 | 0 | +372 | +97 | +1,576 |
| CommutativeCancellation | -68 | 0 | 0 | 0 | 0 | 0 |
| Others | 0 | 0 | 0 | 0 | 0 | 0 |

**Note**: ConsolidateBlocks collapses gate blocks into abstract unitaries (reducing gate count), then UnitarySynthesis expands those unitaries back into basis gates (adding gates back). The net reduction is ConsolidateBlocks removal minus UnitarySynthesis re-addition.

### Key Takeaways

1. **The loop does almost nothing after iteration 1.** Only QFT gets 16 extra 2Q gates (0.2% of total reduction) from the loop. All other circuits get zero benefit from iterating.

2. **ConsolidateBlocks dominates** — 51-75% of optimization time. It also does most of the useful work (reducing 2Q gates by consolidating blocks into optimal unitaries via KAK/Weyl decomposition).

3. **UnitarySynthesis adds gates back** — it resynthesizes the consolidated unitaries into basis gates. The net reduction = ConsolidateBlocks removal - UnitarySynthesis re-addition.

4. **RemoveIdentityEquivalent and ContractIdleWiresInControlFlow are essentially free but also do nothing** on these circuits.

5. **Iterations 2-6 are pure overhead** — they re-run ConsolidateBlocks (expensive) only to confirm nothing changed or to wait out MinimumPoint's backtrack_depth before convergence.

### Answers to Key Questions

1. **How many loop iterations?** 3-6, but only iteration 1 is productive. The extra iterations exist because MinimumPoint requires either a fixed point (same score twice) or `backtrack_depth=5` consecutive non-improvements before stopping.

2. **Is the pre-loop ConsolidateBlocks redundant?** N/A for level 3 — ConsolidateBlocks is inside the loop, not pre-loop. At level 3 it runs every iteration, which means after iteration 1 converges, iterations 2+ re-run it for nothing.

3. **Does the conditional BasisTranslator ever fire?** No — it never fired on any of the 6 test circuits. All optimization passes preserved the basis gate set.

4. **Would MinimumPoint be better than FixedPoint at level 2?** MinimumPoint is more robust (handles oscillation) but also more expensive — it requires more iterations to confirm convergence (backtrack_depth=5). For these circuits, FixedPoint would converge faster since no oscillation was observed in the productive work.

5. **Per-pass timing**: ConsolidateBlocks is the clear bottleneck at 51-75%. CommutativeCancellation and Optimize1qGatesDecomposition are roughly equal (6-15% each). UnitarySynthesis varies (11-28%) depending on how many unitaries ConsolidateBlocks produces.

## Next Steps

- [x] Instrument the optimization loop to count iterations per circuit
- [x] Add per-pass timing to measure where time is spent
- [ ] Compare level 2 vs level 3 quality and speed on benchpress circuits
- [ ] Test whether removing the loop (single iteration) degrades gate quality
- [ ] Profile with real chemistry circuits (e.g., fe4s4 LUCJ) that may have different loop behavior
- [ ] Investigate whether reducing MinimumPoint backtrack_depth (e.g., 2 instead of 5) would save time without losing quality
