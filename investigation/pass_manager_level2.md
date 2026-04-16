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

## Next Steps

- [ ] Instrument the optimization loop to count iterations per circuit on benchpress suite
- [ ] Add per-pass timing to measure where time is spent
- [ ] Compare level 2 vs level 3 quality and speed on benchpress circuits
- [ ] Test whether removing the loop (single iteration) degrades gate quality
