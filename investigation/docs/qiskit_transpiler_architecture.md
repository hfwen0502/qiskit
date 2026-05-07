# Qiskit Transpiler Architecture: A Design Guide

## Why This Document

This document explains *why* the Qiskit transpiler is designed the way it is — not just what each piece does, but the design decisions, constraints, and trade-offs that shaped the current architecture. Understanding this context is essential for proposing improvements that work with the system rather than against it.

## The Big Picture: What Problem Does the Transpiler Solve?

A quantum circuit as written by the user is **abstract** — it uses arbitrary qubit labels, arbitrary gates, and assumes all qubits can interact with each other. Real hardware has three constraints:

1. **Connectivity**: Only certain pairs of physical qubits can perform 2-qubit gates (defined by the coupling map)
2. **Gate set**: Only specific gates are natively supported (e.g., `[CZ, SX, RZ, X]` on IBM Eagle/Heron)
3. **Quality**: Gates have error rates; fewer gates = higher success probability

The transpiler transforms the abstract circuit into one that satisfies all hardware constraints while minimizing gate count (especially 2-qubit gates, which have ~10-100x higher error than 1-qubit gates).

---

## The Stage Pipeline

The transpiler is organized as a **6-stage pipeline**. Each stage has a clear responsibility and runs in order:

```
Input Circuit (abstract)
    │
    ▼
┌─────────────────────────────────────────────┐
│  1. INIT — High-level logical optimization  │
│     Simplify the circuit before layout      │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  2. LAYOUT — Virtual → Physical mapping     │
│     Decide which physical qubit each        │
│     virtual qubit becomes                   │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  3. ROUTING — Insert SWAPs                  │
│     Make non-adjacent 2Q gates executable   │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  4. TRANSLATION — Convert to basis gates    │
│     Replace abstract gates with hardware's  │
│     native gate set                         │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  5. OPTIMIZATION — Reduce gate count        │
│     Cancel, merge, and simplify gates       │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  6. SCHEDULING — Timing and delays          │
│     Insert explicit Delay gates for timing  │
└─────────────────────────────────────────────┘
    │
    ▼
Output Circuit (hardware-ready)
```

**Design decision**: Why this order? Because each stage's output is what the next stage needs as input:
- Layout needs a simplified circuit (init reduces noise)
- Routing needs a layout (must know physical qubit positions)
- Translation needs a routed circuit (SWAPs must already be placed)
- Optimization needs basis gates (can only cancel/merge known gate types)
- Scheduling needs the final gate sequence (timing depends on exact gates)

---

## Stage 1: Init — Simplify Before Layout

### What it does

Performs high-level, topology-independent optimizations. This runs before the circuit is bound to hardware, so it operates on the abstract circuit.

### Concrete example

```
Before init:
q0 ──H──H──●──          ← H·H = I (inverse pair)
            │
q1 ─────────⊕──RZ(0)──  ← RZ(0) = I (identity)

After init:
q0 ──●──
     │
q1 ───⊕──
```

Passes (Level 2):
- **InverseCancellation**: Removes adjacent inverse pairs (H·H, S·Sdg, CX·CX)
- **RemoveDiagonalGatesBeforeMeasure**: Z-rotations before measurement don't affect outcome
- **RemoveIdentityEquivalent**: Removes gates with angle ≈ 0
- **CommutativeCancellation**: Cancels/merges gates that commute
- **ConsolidateBlocks + Split2QUnitaries**: Consolidates 2Q blocks, splits separable ones back into 1Q gates

### Why do optimization here AND in stage 5?

The init stage works on the **abstract** circuit — before routing adds SWAPs. A simpler circuit means fewer 2-qubit gates to route, which means fewer SWAPs inserted, which cascades into a much better final result.

The optimization stage (5) works on the **physical** circuit — after routing. It cleans up the mess that routing created (redundant SWAPs, unnecessary gates from translation).

**Key insight**: Simplifying before routing has outsized impact because routing amplifies circuit complexity. One unnecessary CX before routing might cause 3+ SWAP insertions (each SWAP = 3 CX on typical hardware).

---

## Stage 2: Layout — Virtual to Physical Qubit Mapping

### The problem

```
Virtual circuit:         Hardware coupling map (heavy-hex excerpt):
q0 ──●──                     0 ─ 1 ─ 2
     │                           |
q1 ───⊕──●──                    3
          │                      |
q2 ────────⊕──              4 ─ 5 ─ 6
```

Virtual qubits [q0, q1, q2] must be assigned to physical qubits [0..6] such that 2-qubit gates land on connected pairs. Bad layout = many SWAPs. Good layout = few or zero SWAPs.

### The solution: layered fallback

The layout stage uses a **fallback chain** — try the faster/better method first, fall back if it fails:

```
VF2Layout (exact subgraph isomorphism)
    │
    │ if no perfect layout found within call_limit
    ▼
SabreLayout (heuristic bidirectional search)
```

**VF2Layout**: Searches for a "perfect" layout — one where every 2-qubit gate is already on adjacent physical qubits, requiring zero SWAPs. Uses the VF2 graph isomorphism algorithm. Fast for small circuits but exponential worst-case.

**SabreLayout**: When VF2 can't find a perfect layout (common for large circuits), falls back to a heuristic that simultaneously searches for a good layout AND does routing. Runs multiple random trials, picks the best.

### VF2 call limits by level

| Level | VF2 call_limit | SabreLayout trials |
|-------|:--------------:|:------------------:|
| 0 | (none — skipped) | 5 |
| 1 | 50,000 | 5 |
| 2 | 5,000,000 | 20 |
| 3 | 30,000,000 | 20 |

**Design decision**: Higher optimization levels spend more time searching for better layouts because a good layout can save orders of magnitude more gate overhead than any later optimization.

### How SabreLayout works (the forward-backward heuristic)

SabreLayout's key insight: **a good initial layout produces a good final layout after routing, and vice versa**.

```
Iteration 1:
  Random layout → Route forward → Get final_layout₁
  
Iteration 2:
  final_layout₁ → Route reversed circuit backward → Get final_layout₂

Iteration 3:
  final_layout₂ → Route forward → Get final_layout₃
  ...

After max_iterations: pick the layout that produced fewest SWAPs
```

Each forward-backward pair refines the layout. The algorithm converges quickly — typically 2-4 iterations suffice.

**Multi-trial**: Runs `layout_trials` (default = CPU cores) independent random starts in parallel. Each trial does `max_iterations` forward-backward passes. Best trial wins.

### Concrete example

```
Circuit: q0-q1 CX, q1-q2 CX, q0-q2 CX (triangle)
Hardware: linear chain 0-1-2

Layout A: q0→0, q1→1, q2→2
  q0-q1 CX: physical 0-1 ✓ (adjacent)
  q1-q2 CX: physical 1-2 ✓ (adjacent)
  q0-q2 CX: physical 0-2 ✗ (not adjacent, need SWAP)

Layout B: q0→1, q1→0, q2→2
  q0-q1 CX: physical 1-0 ✓ (adjacent)
  q1-q2 CX: physical 0-2 ✗ (not adjacent, need SWAP)
  q0-q2 CX: physical 1-2 ✓ (adjacent)

Both layouts need 1 SWAP — on a linear chain, a triangle always needs at least 1.
```

---

## Stage 3: Routing — Insert SWAPs for Non-Adjacent Gates

### The problem

After layout, some 2-qubit gates still target non-adjacent physical qubits. Routing inserts SWAP gates to move qubit states to adjacent positions.

```
Before routing:                After routing:
q0(phys 0) ──●──              q0(phys 0) ──╳──●──
             │                              │  │
q1(phys 1) ──┼──              q1(phys 1) ──╳──┼──
             │                                 │
q2(phys 2) ───⊕──            q2(phys 2) ───────⊕──

Gate: CX(q0, q2) needs 0-2 adjacent.
SWAP(0,1) moves q0's state to position 1. Now CX(1,2) is adjacent.
```

**Cost of a SWAP**: 1 SWAP = 3 CX gates (or 3 CZ gates). This is why routing is the dominant cost — each inserted SWAP adds 3 expensive 2-qubit gates.

### SABRE routing algorithm

SABRE (SWAP-based Bidirectional heuristic search):

1. Maintain a **front layer** — gates whose dependencies are satisfied
2. If any front-layer gate is on adjacent qubits → execute it, advance front layer
3. Otherwise, evaluate all possible SWAPs on qubits in the front layer
4. Score each SWAP candidate using a heuristic:
   - **Basic**: `H = Σ distance(phys[q1], phys[q2])` for all front-layer gates
   - **Lookahead**: Also considers upcoming gates with decaying weight
   - **Decay**: Penalizes recently-swapped qubits to reduce depth
5. Insert the best SWAP, update qubit mapping, repeat

### Key constants

```python
# From SabreLayout heuristic configuration
heuristic = (
    Heuristic(attempt_limit=10 * num_qubits)
    .with_basic(1.0, SetScaling.Constant)
    .with_lookahead([0.5 / num_qubits], SetScaling.Constant)
    .with_decay(0.001, 5)
)
```

- `attempt_limit = 10 * num_qubits`: Max SWAP evaluations before escape mechanism
- Basic weight: 1.0 (dominant factor)
- Lookahead weight: 0.5/n_qubits (diminishing for larger circuits)
- Decay: increment 0.001, reset every 5 swaps

### Why routing dominates compile time

From our profiling: **SABRE routing = 69% of total transpile time** at Level 2. This is because:
1. For each gate in the front layer, evaluate all neighbor SWAPs (O(degree) per qubit)
2. Score each SWAP by computing distances for all front-layer gates
3. Multiple trials (20 at Level 2) run the full algorithm independently
4. Forward-backward iterations (2 at Level 2) double the work

For a 100-qubit circuit with 10,000 2Q gates on heavy-hex (degree 2-3), this is substantial computation — but it's already highly optimized in Rust with rayon parallelism.

---

## Stage 4: Translation — Convert to Native Gate Set

### The problem

After routing, the circuit contains abstract gates (CX, H, RZ, SWAP) that may not be in the hardware's native set. Translation converts every gate to the target basis.

```
Target basis: [CZ, SX, RZ, X]

Before translation:          After translation:
q0 ──H──●──                  q0 ──RZ(π/2)──SX──RZ(π/2)──●──
        │                                                 │
q1 ──────⊕──                 q1 ──────────────────────────Z──RZ──

(H = RZ(π/2)·SX·RZ(π/2), CX decomposed via CZ + 1Q gates)
```

### How BasisTranslator works

Uses an **equivalence library** — a database of known gate decompositions:
1. Build a graph where nodes are gate sets and edges are decomposition rules
2. Dijkstra search from source gate set to target gate set
3. Apply the decomposition chain

**Design decision**: Why not just hard-code decompositions? Because different backends have different native gates (CX vs CZ vs ECR vs iSWAP), and the equivalence library approach is extensible — new backends just register their rules.

---

## Stage 5: Optimization — The Loop

### Why optimization comes AFTER routing and translation

At this point, the circuit is in final form — all gates are native, all connectivity is satisfied. But routing and translation introduced redundancy:

- SWAPs decomposed into 3 CX → adjacent CX pairs might cancel
- Translation added extra 1Q gates → chains can be merged
- Near-identity gates appeared → can be removed

### The Level 2 optimization structure

```
Pre-loop (run once):
  ConsolidateBlocks → UnitarySynthesis

Loop (repeat until converged):
  RemoveIdentityEquivalent → Optimize1qGatesDecomposition →
  CommutativeCancellation → ContractIdleWiresInControlFlow
```

### Pass 1: ConsolidateBlocks (pre-loop)

**What**: Finds consecutive 2-qubit gates on the same pair of qubits, multiplies their unitary matrices, and replaces the block with a single `UnitaryGate`.

```
Before:                              After:
q0 ──CZ──RZ(θ)──CZ──RZ(φ)──        q0 ──[U]──
     │           │                        │
q1 ──Z───────────Z───────────        q1 ──[U]──

The 4 gates → single 4×4 unitary matrix U
```

**Why pre-loop**: This is the heaviest optimization — it recomputes optimal decompositions for 2-qubit blocks. Only needs to run once because later loop passes don't create new 2-qubit blocks.

### Pass 2: UnitarySynthesis (pre-loop)

**What**: Takes the consolidated unitary matrices and decomposes them into the minimum number of basis 2-qubit gates using KAK/Weyl decomposition.

```
Before:                         After (KAK decomposition):
q0 ──[4×4 Unitary]──            q0 ──RZ──RY──●──RZ──
     │                                       │
q1 ──[4×4 Unitary]──            q1 ──RZ──────⊕──RY──

Any 2-qubit unitary needs at most 3 CX (or CZ) gates.
Many common patterns need only 0-1.
```

**The KAK decomposition** factors any 2-qubit unitary as:
```
U = (A₁ ⊗ B₁) · exp(i[α·XX + β·YY + γ·ZZ]) · (A₂ ⊗ B₂)
```
Where A, B are 1-qubit unitaries and α, β, γ determine how many CX gates are needed:
- α=β=γ=0 → 0 CX (separable)
- Only α≠0 → 1 CX
- α,β≠0 → 2 CX
- General → 3 CX

**This is where the big 2Q gate reductions happen.** A block of 4 CX gates might decompose to 1-2 CX via KAK.

### Pass 3: RemoveIdentityEquivalent (loop)

**What**: Removes gates that are effectively the identity (angle ≈ 0 or unitary ≈ I).

```
Before:                    After:
q0 ──RZ(1e-15)──CZ──      q0 ──CZ──
                 │              │
q1 ───────────────Z──      q1 ───Z──
```

**How it detects identity**: Computes average gate fidelity. If `1 - fidelity < tolerance`, the gate is identity-equivalent.

**Why it helps**: UnitarySynthesis sometimes produces near-zero rotations. Also, gate cancellations in CommutativeCancellation can leave rotation gates with angle ≈ 2π (equivalent to identity with global phase).

### Pass 4: Optimize1qGatesDecomposition (loop)

**What**: Finds consecutive runs of 1-qubit gates on the same qubit, computes their combined unitary, and replaces with the optimal Euler decomposition.

```
Before:                              After (ZSX basis):
q0 ──RZ(a)──SX──RZ(b)──SX──RZ(c)──SX──RZ(d)──    q0 ──RZ(α)──SX──RZ(β)──

7 gates → 3 gates (same unitary, minimal form)
```

**Euler decomposition**: Any 1-qubit unitary can be written as at most 3 rotations in a chosen basis:
- ZYZ: `U = RZ(γ)·RY(β)·RZ(α)`
- ZSX: `U = RZ(γ)·SX·RZ(β)·SX·RZ(α)` (at most)
- The pass tries all available bases and picks the one with fewest gates / lowest error

**Important behavior**: This pass mutates the DAG **unconditionally** — it always replaces 1Q runs with their optimal decomposition, even when the result is identical to the input. This is why a naive "did-anything-change" loop condition doesn't work with this pass.

### Pass 5: CommutativeCancellation (loop)

**What**: Two operations:
1. **Cancel** self-inverse 2Q gate pairs: CX·CX = I, CZ·CZ = I (even with commuting gates between them)
2. **Consolidate** 1Q Z-rotations: RZ(a)·...·RZ(b) → RZ(a+b) when intervening gates commute with RZ

```
Cancellation example:
Before:  q0 ──●──RZ(θ)──●──     After:  q0 ──RZ(θ)──
              │          │
         q1 ───⊕──────────⊕──          q1 ─────────────
CX·CX = I (RZ commutes with CX control)

Consolidation example:
Before:  q0 ──RZ(π/4)──H──RZ(π/4)──   After: q0 ──RZ(π/4)──H──RZ(π/4)──
(H does NOT commute with RZ, so no consolidation here)

Before:  q0 ──RZ(π/4)──X──RZ(π/4)──   After: (depends on commutation rules)
```

**Commutativity checking**: Uses a pre-computed library (`CommutationChecker`) with known commutation relations for standard gates. Only checks gates on ≤ 3 qubits (constant `MAX_NUM_QUBITS = 3`).

### The cross-iteration interaction

The loop exists because passes can create opportunities for each other:

```
Iteration 1:
  CommutativeCancellation consolidates RZ(π/4) + RZ(π/4) → RZ(π/2)
  This shortens a 1Q run on that qubit

Iteration 2:
  Optimize1qGatesDecomposition sees the shorter run
  Can now decompose [RZ(π/2), SX, RZ(θ)] more efficiently than
  the original [RZ(π/4), SX, RZ(π/4), SX, RZ(θ)]
  Saves 1-2 gates
```

This is the mechanism that the dev team identified: **CommutativeCancellation's rotation consolidation creates opportunities for Optimize1qGatesDecomposition on the next iteration.** However, this only affects rotation-heavy circuits (QFT, QAOA) and the benefit is small (0.02-0.18% total gates).

### Loop convergence

**Current approach (FixedPoint)**: Compute total gate count and depth after each iteration. Loop until both are unchanged. Requires minimum 2 iterations (one to do work, one to confirm).

**Our proposed approach (changed-flag)**: Each pass reports whether it modified multi-qubit gates. If no pass did, exit immediately. Saves the confirmation iteration.

---

## Stage 6: Scheduling

Inserts explicit `Delay` instructions to account for hardware timing. Not relevant to gate optimization — included for completeness.

---

## How Passes Communicate

### The property_set

A shared dictionary that persists across all passes in the pipeline. Passes can read from it and write to it.

Key fields:

| Key | Written by | Read by | Purpose |
|-----|-----------|---------|---------|
| `layout` | Layout passes | Routing, ApplyLayout | Virtual→physical mapping |
| `final_layout` | SabreLayout/Routing | VF2PostLayout | Final permutation after SWAPs |
| `all_gates_in_basis` | GatesInBasis | ConditionalController | Skip translation if already in basis |
| `VF2Layout_stop_reason` | VF2Layout | Fallback logic | Did VF2 succeed? |
| `depth_fixed_point` | FixedPoint("depth") | DoWhileController | Convergence signal |
| `size_fixed_point` | FixedPoint("size") | DoWhileController | Convergence signal |

### DAG mutations

Passes receive a `DAGCircuit` — a directed acyclic graph where:
- **Nodes** = quantum operations (gates) + input/output placeholders
- **Edges** = qubit/clbit wires connecting operations in dependency order

Passes mutate the DAG in-place:
- `dag.remove_op_node(node)` — remove a gate
- `dag.substitute_node(node, new_op)` — replace a gate
- `dag.substitute_node_with_dag(node, sub_dag)` — expand a gate into a sub-circuit

### Flow controllers

- **DoWhileController**: Runs a list of passes, then checks a condition. Repeats until condition returns False.
- **ConditionalController**: Runs passes only if condition is True (e.g., skip translation if circuit is already in basis).

---

## Optimization Levels: The Trade-off

| Aspect | Level 0 | Level 1 | Level 2 | Level 3 |
|--------|---------|---------|---------|---------|
| **Init** | Minimal unroll | + InverseCancellation | + ConsolidateBlocks, Split2Q | Same as L2 |
| **Layout** | Trivial | Dense → SabreLayout | VF2 → SabreLayout | VF2 (exhaustive) → SabreLayout |
| **VF2 effort** | — | 50K calls | 5M calls | 30M calls |
| **SABRE trials** | 5 | 5 | 20 | 20 |
| **SABRE iterations** | 1 | 2 | 2 | 4 |
| **Optimization** | None | Light loop | Pre-loop + loop | Full loop (ConsolidateBlocks in loop) |
| **Convergence** | — | FixedPoint | FixedPoint | MinimumPoint (with backtracking) |
| **Post-opt** | — | — | — | VF2PostLayout |
| **Time** | Fastest | Fast | Moderate | Slowest |
| **Quality** | Worst | Good | Very good | Best (sometimes) |

**Design decision**: Level 2 is the default because it provides the best quality/speed trade-off for most circuits. Level 3 is only better for circuits where the optimization loop can find multi-qubit improvements through repeated ConsolidateBlocks+UnitarySynthesis (rare in practice).

### Level 3's MinimumPoint vs Level 2's FixedPoint

- **FixedPoint**: Stop when metrics don't change. Greedy — accepts the first stable point.
- **MinimumPoint**: Track the best metrics seen so far. Allow the loop to run beyond the first stable point (up to `backtrack_depth=5` iterations without improvement), then backtrack to the best. Handles cases where the optimization landscape has local minima.

---

## The Plugin System

Each stage is implemented as a **plugin** — an entry point that returns a PassManager for that stage. This allows:
- External packages to provide custom stages (e.g., a custom routing algorithm)
- Level-aware tuning (same plugin, different parameters per level)
- Clean separation of concerns

```python
class PassManagerStagePlugin(ABC):
    @abstractmethod
    def pass_manager(self, pass_manager_config, optimization_level=None):
        """Return the PassManager for this stage."""
```

Built-in plugins are registered in `pyproject.toml`:
```toml
[project.entry-points."qiskit.transpiler.routing"]
"sabre" = "qiskit.transpiler.preset_passmanagers.builtin_plugins:..."
```

---

## The DAG: Why Not a List of Instructions?

A quantum circuit could be represented as a flat list of instructions. The DAG representation is chosen because:

1. **Dependency analysis**: Instantly know which gates can run in parallel (no shared qubits)
2. **Topological flexibility**: Gates can be reordered freely within dependency constraints
3. **Efficient mutations**: Insert/remove/replace gates without renumbering everything
4. **Depth computation**: Longest path through the DAG = circuit depth
5. **Block collection**: Find maximal sequences on the same qubits (for ConsolidateBlocks)

This is the same design used in classical compiler IRs (LLVM's SSA, GCC's GIMPLE) — proven effective for optimization passes.

---

## Key Design Principles

1. **Separate concerns by stage**: Each stage has one job. Layout doesn't optimize. Optimization doesn't route.

2. **Fail-fast fallback chains**: Try the best method first (VF2), fall back gracefully (SabreLayout). Don't waste time on methods that won't succeed.

3. **Rust for hot paths, Python for orchestration**: All computationally intensive passes (SABRE, CommutativeCancellation, Optimize1qGates) are in Rust. Python handles pipeline assembly and control flow. Python overhead is <2% of total time.

4. **Multi-trial parallelism**: Heuristic algorithms (SABRE) run multiple independent trials with different random seeds. More trials = better results, and trials are embarrassingly parallel.

5. **The optimization loop is conservative**: It runs until convergence (nothing changes) rather than for a fixed number of iterations. This guarantees the result is a local optimum but costs an extra "confirmation" iteration.

6. **Pre-loop vs loop separation**: Expensive passes that only need to run once (ConsolidateBlocks + UnitarySynthesis at Level 2) go in the pre-loop. Cheap passes that might benefit from iteration go in the loop.

7. **Level-based trade-offs**: Users choose their time/quality trade-off. The transpiler doesn't guess — it provides clear levels with predictable behavior.

---

## Source File Reference

| Concept | File |
|---------|------|
| Stage assembly | `qiskit/transpiler/preset_passmanagers/generate_preset_pass_manager.py` |
| StagedPassManager | `qiskit/transpiler/passmanager.py` |
| Plugin system | `qiskit/transpiler/preset_passmanagers/plugin.py` |
| All built-in plugins | `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` |
| Level definitions | `qiskit/transpiler/preset_passmanagers/level{0,1,2,3}.py` |
| Flow controllers | `qiskit/passmanager/flow_controllers.py` |
| Property set | `qiskit/passmanager/compilation_status.py` |
| DAG representation | `crates/circuit/src/dag_circuit.rs` (Rust), `qiskit/dagcircuit/` (Python) |
| SabreLayout | `qiskit/transpiler/passes/layout/sabre_layout.py` + `crates/transpiler/src/passes/sabre/layout.rs` |
| SABRE routing | `crates/transpiler/src/passes/sabre/route.rs` |
| SABRE heuristic | `crates/transpiler/src/passes/sabre/heuristic.rs` |
| ConsolidateBlocks | `qiskit/transpiler/passes/optimization/consolidate_blocks.py` |
| UnitarySynthesis | `qiskit/transpiler/passes/synthesis/unitary_synthesis.py` |
| Optimize1qGatesDecomposition | `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` |
| CommutativeCancellation | `crates/transpiler/src/passes/commutation_cancellation.rs` |
| RemoveIdentityEquivalent | `crates/transpiler/src/passes/remove_identity_equiv.rs` |
| BasisTranslator | `qiskit/transpiler/passes/basis/basis_translator.py` |
| CouplingMap | `qiskit/transpiler/coupling.py` |
| Target | `qiskit/transpiler/target.py` |

---
---

# Part 2: Implementation Details

This section covers the engineering internals — how the transpiler is built, how Rust and Python interact, and the patterns you need to understand before contributing changes.

---

## The Rust/Python Boundary (PyO3)

### Why Rust?

All computationally intensive transpiler passes are implemented in Rust. Python handles orchestration (pipeline assembly, flow control, user API). This is not optional — Python is ~100x slower for tight loops over graph nodes.

The boundary uses **PyO3** — a Rust crate that generates Python bindings at compile time. The compiled Rust code ships as `qiskit._accelerate` (a native extension module).

### The pattern: every Rust pass has three layers

```
┌─────────────────────────────────────────────────┐
│  Python wrapper (TransformationPass)            │
│  qiskit/transpiler/passes/optimization/foo.py   │
│                                                 │
│  class FooPass(TransformationPass):             │
│      def run(self, dag):                        │
│          foo_function(dag, self.params)          │
│          return dag                             │
└────────────────────┬────────────────────────────┘
                     │ calls into native module
                     ▼
┌─────────────────────────────────────────────────┐
│  PyO3 bridge function (#[pyfunction])           │
│  crates/transpiler/src/passes/foo.rs            │
│                                                 │
│  #[pyfunction]                                  │
│  pub fn py_foo(                                 │
│      py: Python,                                │
│      dag: &mut DAGCircuit,                      │
│  ) -> PyResult<()> {                            │
│      py.detach(|| run_foo(dag))?;               │
│      Ok(())                                     │
│  }                                              │
└────────────────────┬────────────────────────────┘
                     │ GIL released, can use rayon
                     ▼
┌─────────────────────────────────────────────────┐
│  Pure Rust implementation                       │
│                                                 │
│  pub fn run_foo(dag: &mut DAGCircuit)           │
│      -> PyResult<()> {                          │
│      // Actual algorithm here                   │
│      // Can use rayon for parallelism           │
│  }                                              │
└─────────────────────────────────────────────────┘
```

**Key details:**
- `dag: &mut DAGCircuit` — the DAG is passed as a mutable reference, not copied. Rust mutates the same object Python holds.
- `py.detach(|| ...)` — releases the Python GIL before entering the Rust implementation. This is **critical** — without it, rayon threads would deadlock on the GIL.
- `PyResult<T>` — wraps return values in Python exception handling.

### Module registration

Every Rust pass must be registered in three places:

```rust
// 1. crates/transpiler/src/passes/foo.rs — define the module function
pub fn foo_mod(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_wrapped(wrap_pyfunction!(py_foo))?;
    Ok(())
}

// 2. crates/transpiler/src/passes/mod.rs — export it
pub use foo::{foo_mod, run_foo};

// 3. crates/pyext/src/lib.rs — register as submodule
add_submodule(m, ::qiskit_transpiler::passes::foo_mod, "foo")?;
```

Then Python imports: `from qiskit._accelerate.foo import foo`

---

## DAGCircuit Internals

### The graph library

DAGCircuit is built on `rustworkx_core::petgraph::StableDiGraph<NodeType, Wire>` — a stable directed graph from the petgraph library (via rustworkx-core).

"Stable" means node indices remain valid after removal — critical because passes hold `NodeIndex` values while modifying the graph.

### Core data structure

```rust
pub struct DAGCircuit {
    // The graph itself
    dag: StableDiGraph<NodeType, Wire>,

    // Qubit/Clbit registries (name → index mapping)
    qubits: ObjectRegistry<Qubit, ShareableQubit>,
    clbits: ObjectRegistry<Clbit, ShareableClbit>,

    // Fast I/O node lookup: qubit index → [input_node, output_node]
    qubit_io_map: Vec<[NodeIndex; 2]>,
    clbit_io_map: Vec<[NodeIndex; 2]>,

    // Interning for memory efficiency
    qargs_interner: Interner<[Qubit]>,   // Deduplicates qubit tuples
    cargs_interner: Interner<[Clbit]>,   // Deduplicates clbit tuples

    // Metadata
    global_phase: Param,
    op_names: IndexMap<String, usize>,   // Operation name → count
}
```

### Node types

```rust
enum NodeType {
    QubitIn(Qubit),           // Input placeholder for a qubit wire
    QubitOut(Qubit),          // Output placeholder for a qubit wire
    ClbitIn(Clbit),           // Same for classical bits
    ClbitOut(Clbit),
    Operation(PackedInstruction),  // Actual gate/operation
}
```

### Wire model

Each qubit has a **wire** — a chain of nodes from `QubitIn` → operations → `QubitOut`. Edges are labeled with `Wire::Qubit(q)` or `Wire::Clbit(c)` to identify which wire they belong to.

```
QubitIn(0) ──Wire::Qubit(0)──> CX(0,1) ──Wire::Qubit(0)──> RZ(0) ──Wire::Qubit(0)──> QubitOut(0)
QubitIn(1) ──Wire::Qubit(1)──> CX(0,1) ──Wire::Qubit(1)──> QubitOut(1)
```

### PackedInstruction and interning

Operations are stored as `PackedInstruction`:
```rust
struct PackedInstruction {
    op: PackedOperation,          // The gate (StandardGate enum or Python object)
    qubits: Interned<[Qubit]>,    // Interned qubit indices (u32 key)
    clbits: Interned<[Clbit]>,    // Interned clbit indices (u32 key)
    params: Option<Box<SmallVec<[Param; 3]>>>,  // Gate parameters
    // ...
}
```

**Interning**: Instead of storing `Vec<Qubit>` per gate (heap allocation), common qubit tuples like `[q0, q1]` are deduplicated. The interner maps each unique tuple to a 32-bit index. This saves ~4x memory on 64-bit systems for circuits with many repeated qubit patterns.

### Key DAG methods used by passes

| Method | What it does | Used by |
|--------|-------------|---------|
| `nodes_on_wire(wire, only_ops)` | Walk a single qubit wire, return nodes in order | CommutationAnalysis |
| `collect_1q_runs()` | Find maximal sequences of consecutive 1Q gates | Optimize1qGatesDecomposition |
| `collect_2q_runs()` | Find maximal sequences of consecutive 2Q gate pairs | ConsolidateBlocks |
| `remove_op_node(node)` | Remove a gate, reconnect predecessor→successor wires | All cancellation passes |
| `substitute_node_with_dag(node, sub_dag)` | Replace a gate with a sub-circuit | UnitarySynthesis, BasisTranslator |
| `op_nodes(include_directives)` | Iterate over all operation nodes | RemoveIdentityEquivalent |
| `get_qargs(interned)` | Resolve interned qubit key → `&[Qubit]` | All passes |
| `num_qubits()` | Number of qubits in circuit | Threshold checks |

---

## Rayon Parallelism

### Threading control

Two environment variables control parallelism:

```rust
// crates/util/src/lib.rs
pub fn getenv_use_multiple_threads() -> bool {
    let parallel_context = env::var("QISKIT_IN_PARALLEL") == Ok("TRUE");
    let force_threads = env::var("QISKIT_FORCE_THREADS") == Ok("TRUE");
    !parallel_context || force_threads
}
```

- `RAYON_NUM_THREADS=N` — sets the rayon thread pool size (default = CPU cores)
- `QISKIT_IN_PARALLEL=TRUE` — disables threading (prevents nested parallelism when caller is already parallel)
- `QISKIT_FORCE_THREADS=TRUE` — overrides `QISKIT_IN_PARALLEL`, forces threads on

### Parallelism pattern: threshold + conditional

Passes don't always parallelize — there's overhead in thread spawning. Each pass uses a threshold:

```rust
const PARALLEL_THRESHOLD: usize = 50_000;  // Number of op nodes

if dag.op_nodes(false).count() > PARALLEL_THRESHOLD && getenv_use_multiple_threads() {
    // Parallel path using rayon
    dag.op_nodes(false)
        .into_par_iter()
        .filter_map(|node| process_node(node))
        .collect::<Vec<_>>()
} else {
    // Sequential fallback
    dag.op_nodes(false)
        .filter_map(|node| process_node(node))
        .collect::<Vec<_>>()
}
```

### Which passes use rayon (on main branch)

| Pass | Threshold | What's parallelized |
|------|-----------|-------------------|
| RemoveIdentityEquivalent | 50,000 ops | Identity detection across all nodes |
| Optimize1qGatesDecomposition | 50,000 ops | 1Q run decomposition |
| SabreLayout | >1 trial | Layout trials run in parallel |
| SabreSwap (within SabreLayout) | >1 trial | Swap trials run in parallel |
| DenseLayout | (always if threads enabled) | Subgraph scoring |
| BarrierBeforeFinalMeasurement | 150 qubits | Final node detection |

### SabreLayout's two-level parallelism

SABRE has **nested** parallelism using `rayon_cond::CondIterator`:

```rust
// Level 1: layout trials in parallel
CondIterator::new(
    seeds(num_layout_trials),
    allow_parallel && num_layout_trials > 1,
)
.map(|seed| layout_trial(problem, seed, num_swap_trials, allow_parallel, ...))
.min_by_key(|result| result.swap_count())

// Level 2 (inside each layout_trial): swap trials in parallel
CondIterator::new(
    seeds(num_swap_trials),
    allow_parallel && num_swap_trials > 1,
)
.map(|seed| swap_map_trial(problem, &initial_layout, seed))
.min_by_key(|result| result.swap_count())
```

`CondIterator` from the `rayon_cond` crate: if the condition is `true`, it uses `rayon::par_iter`; if `false`, it uses normal sequential iteration. Same code path, conditional parallelism.

---

## SABRE Internals

### The compressed routing DAG (SabreDAG)

SABRE doesn't route on the full DAGCircuit. It builds a compressed **interaction DAG** that only contains routing-relevant operations:

```rust
pub enum InteractionKind {
    Synchronize,                     // 1Q gates, barriers (ordering only)
    TwoQ([VirtualQubit; 2]),        // 2Q gates (the actual routing targets)
    ControlFlow(Box<[(SabreDAG, DAGCircuit)]>),
}
```

1Q gates are folded into synchronization barriers. Consecutive 2Q gates on the same qubit pair are combined. This dramatically reduces the graph size and speeds up the routing loop.

### The routing state

```rust
struct State {
    layout: NLayout,                              // Current virtual→physical mapping
    front_layer: Layer,                           // 2Q gates ready to route
    lookahead_layers: Box<[Layer]>,               // Future gates for lookahead scoring
    required_predecessors: VecMap<NodeIndex, u32>, // Dependency counter per node
    decay: VecMap<PhysicalQubit, f64>,            // Per-qubit swap penalty
    rng: Pcg64Mcg,                                // Per-trial deterministic RNG
}
```

### The main routing loop

```
while front_layer is not empty:
    1. Score all candidate SWAPs (neighbors of front-layer qubits)
    2. Pick the best SWAP (lowest score = shortest resulting distances)
    3. Apply SWAP to layout (update virtual→physical mapping)
    4. Check if any front-layer gate is now routable (both qubits adjacent)
    5. If routable: execute gate, advance front layer
    6. If stuck after attempt_limit SWAPs: escape via greedy shortest-path
```

### SWAP scoring formula

For each candidate SWAP on edge `(p, q)`:

```
score = basic_weight × Σ[distance(layout[g.q1], layout[g.q2]) for g in front_layer]
      + lookahead_weight × Σ[same for lookahead layers]

If decay enabled:
    score *= max(decay[p], decay[q])
```

The score measures "how close are front-layer gates to being executable after this SWAP?" Lower is better.

### The distance matrix

Precomputed all-pairs shortest paths on the coupling graph:

```rust
pub struct RoutingTarget {
    pub neighbors: Neighbors,       // Sparse adjacency list
    pub distance: Array2<f64>,      // [num_qubits × num_qubits] shortest-path matrix
}
```

O(1) distance lookup during scoring. Computed once via BFS at pass initialization.

### Hard-coded IBM device layouts

SABRE includes special starting layouts for specific IBM device sizes:

```rust
if num_physical_qubits == 127 {  // IBM Eagle
    starting_layouts.push(precomputed_ring_127);
}
if num_physical_qubits == 133 {  // IBM Heron
    starting_layouts.push(precomputed_ring_133);
}
if num_physical_qubits == 156 {  // IBM Heron extended
    starting_layouts.push(precomputed_ring_156);
}
```

These are **maximal ring paths** through the coupling graph, computed offline via `max(simple_cycles(graph), key=len)`. They provide a good starting layout for chain-like circuits on heavy-hex topology.

**Limitation**: Only these three sizes get the ring heuristic. Other device sizes (or non-IBM topologies) start with random layouts + a dense layout + forward/reverse identity layouts.

---

## The Pass Contract

### TransformationPass vs AnalysisPass

```python
# qiskit/transpiler/basepasses.py

class AnalysisPass(BasePass):
    """Reads the DAG, writes to property_set. Must NOT modify the DAG."""
    # Example: Size, Depth, FixedPoint, GatesInBasis

class TransformationPass(BasePass):
    """Modifies and returns the DAG. Should NOT write to property_set
    (except via the changed-flag pattern)."""
    # Example: RemoveIdentityEquivalent, Optimize1qGatesDecomposition
```

Both implement `run(self, dag: DAGCircuit)`:
- AnalysisPass: returns `None` (DAG unchanged)
- TransformationPass: returns modified `DAGCircuit`

### The property_set contract

- AnalysisPasses write to `self.property_set[key] = value`
- TransformationPasses read from `self.property_set[key]`
- The property_set persists across all passes in the pipeline
- Reset at the start of each `PassManager.run()` call

### Pass equivalence (MetaPass)

Passes are hashed by their constructor arguments:
```python
class MetaPass(type):
    # Two passes with identical __init__ args are considered equivalent
    # Equivalent passes are deduplicated in the pipeline
```

This prevents accidentally running the same pass twice with the same parameters.

---

## Build System

### Building Qiskit from source

```bash
# Default (debug mode — fast compile, slow runtime)
pip install -e .

# Release mode (slow compile, optimized runtime — REQUIRED for benchmarks)
QISKIT_BUILD_PROFILE=release pip install -e .
```

**Critical**: Debug builds are 10-20x slower than release for Rust code. All benchmark results must use release builds.

### How the build works

```
pyproject.toml → setup.py → setuptools-rust → cargo build → qiskit._accelerate.so
```

- `setup.py` reads `QISKIT_BUILD_PROFILE` to set `debug=True/False`
- `setuptools-rust` invokes `cargo` with the appropriate profile
- Output: `qiskit/_accelerate.*.so` (native extension)
- PyO3 generates the Python↔Rust binding code at compile time

### Feature flags

```python
# setup.py
if os.getenv("QISKIT_NO_CACHE_GATES") == "1":
    features = []
else:
    features = ["cache_pygates"]  # Default: cache gate matrix computations
```

### Cargo workspace structure

```
crates/
├── pyext/          # The final .so — imports all other crates, registers modules
├── circuit/        # DAGCircuit, PackedInstruction, Qubit/Clbit types
├── transpiler/     # All transpiler passes
│   └── src/passes/ # Individual pass implementations
├── synthesis/      # Gate synthesis algorithms (KAK, Euler, etc.)
└── util/           # Shared utilities (threading control, etc.)
```

---

## Writing a New Pass (Development Workflow)

### Step 1: Rust implementation

Create `crates/transpiler/src/passes/my_new_pass.rs`:

```rust
use pyo3::prelude::*;
use qiskit_circuit::dag_circuit::DAGCircuit;

const PARALLEL_THRESHOLD: usize = 50_000;

/// Pure Rust implementation (no Python dependency)
pub fn run_my_new_pass(dag: &mut DAGCircuit) -> PyResult<bool> {
    let mut changed = false;
    // ... modify dag ...
    Ok(changed)
}

/// PyO3 bridge — releases GIL, calls implementation
#[pyfunction]
#[pyo3(name = "my_new_pass")]
pub fn py_my_new_pass(py: Python, dag: &mut DAGCircuit) -> PyResult<bool> {
    py.detach(|| run_my_new_pass(dag))
}

/// Module registration
pub fn my_new_pass_mod(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_wrapped(wrap_pyfunction!(py_my_new_pass))?;
    Ok(())
}
```

### Step 2: Register the module

In `crates/transpiler/src/passes/mod.rs`:
```rust
mod my_new_pass;
pub use my_new_pass::{my_new_pass_mod, run_my_new_pass};
```

In `crates/pyext/src/lib.rs`:
```rust
add_submodule(m, ::qiskit_transpiler::passes::my_new_pass_mod, "my_new_pass")?;
```

### Step 3: Python wrapper

Create `qiskit/transpiler/passes/optimization/my_new_pass.py`:

```python
from qiskit.transpiler.basepasses import TransformationPass
from qiskit._accelerate.my_new_pass import my_new_pass

class MyNewPass(TransformationPass):
    def __init__(self, some_param=1.0):
        super().__init__()
        self._some_param = some_param

    def run(self, dag):
        changed = my_new_pass(dag)
        if changed:
            # Optional: signal to loop controller
            self.property_set["_opt_pass_changed"] = True
        return dag
```

### Step 4: Add to the pipeline

In `qiskit/transpiler/preset_passmanagers/builtin_plugins.py`:
```python
from qiskit.transpiler.passes import MyNewPass

# Add to appropriate stage/level
loop = [
    RemoveIdentityEquivalent(...),
    MyNewPass(some_param=0.5),  # New pass here
    Optimize1qGatesDecomposition(...),
    CommutativeCancellation(...),
]
```

### Step 5: Build and test

```bash
# Build with release optimizations
QISKIT_BUILD_PROFILE=release pip install -e .

# Run relevant tests
python -m pytest test/python/transpiler/ -x -k "my_new_pass"

# Run full preset passmanager tests (checks all levels still work)
python -m pytest test/python/transpiler/test_preset_passmanagers.py -x
```

---

## Performance Considerations

### Why SABRE dominates (~69% of compile time)

1. **Combinatorial search**: For each gate, evaluate all neighbor SWAPs × trials × iterations
2. **Multi-trial**: 20 layout trials × 20 swap trials = up to 400 independent routing runs
3. **Forward-backward**: 2 iterations doubles the work
4. **Distance matrix queries**: O(1) per lookup, but millions of lookups

### Where time goes at Level 2 (from our profiling, 100-qubit circuits)

```
SABRE (layout + routing):  69%
ConsolidateBlocks:         12%
UnitarySynthesis:           8%
CommutativeCancellation:    5%
Optimize1qGatesDecomposition: 4%
Everything else:            2%
```

### What this means for optimization work

- **Reducing SABRE time** requires either fewer trials or faster per-trial execution (both in Rust already)
- **The optimization loop** is only 9% of total time — even eliminating it entirely saves at most 9%
- **Python overhead is negligible** (<2%) — no Python-level parallelism opportunities exist
- **The biggest quality lever is layout** — a better initial layout eliminates SWAPs, each worth 3 CX gates
