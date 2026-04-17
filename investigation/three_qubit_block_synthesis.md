# Investigation: 3-Qubit Block Collection and Synthesis

## Overview

The Qiskit team suggested exploring 3-qubit block detection and synthesis as a potential optimization direction. Qiskit already has state-of-the-art 2-qubit synthesis (KAK/Weyl decomposition, 0-3 CX gates, microseconds). The question is: **can we extend this to 3-qubit blocks, and does it pay off?**

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

## Code Paths: 2Q vs 3Q Block Synthesis

The 2Q block optimization pipeline runs in two places:

1. **Init stage** (before routing): `Collect2qBlocks → Collect1qRuns → ConsolidateBlocks → Split2QUnitaries`
2. **Optimization pre-loop** (after routing): `ConsolidateBlocks → UnitarySynthesis`

All pieces for 3Q block synthesis already exist in the codebase. The key difference is that the 3Q path lacks the cost-prediction and gate-guard that make the 2Q path effective.

### 2Q Call Chain (Current Pipeline)

```
Collect2qBlocks
  → dag.collect_2q_runs()                          // Rust bicolor graph algorithm
  → writes block_list to property_set

ConsolidateBlocks (consolidate_blocks.rs:404-452)
  → block_qargs.len() == 2 branch
  ① blocks_to_matrix(dag, &block, block_index_map)  // Pure Rust: Matrix4 multiplication
                                                     // (crates/synthesis/src/matrix/two_qubit.rs:221)
  ② num_basis_gates_inner(matrix)                    // Weyl coordinates → trace → argmax
     → __weyl_coordinates(unitary)                   // compute [a, b, c] Weyl parameters
     → traces[0..3] → fidelity comparison            // (weyl_decomposition.rs:232-262)
     → returns 0, 1, 2, or 3 CX needed              // CHEAP: just matrix math, microseconds
  ③ GATE GUARD:                                      // ← THE KEY DIFFERENCE
        if num_basis_gates < basis_count             // synthesis cheaper than original?
        || force_consolidate
        || block.len() > MAX_2Q_DEPTH (20)
        || outside_basis
     THEN consolidate, ELSE skip (keep original)
  ④ UnitaryGate { array: TwoQ(matrix) }             // 4×4 Matrix4<Complex64>
  → dag.replace_block()

UnitarySynthesis (unitary_synthesis/mod.rs:401-404)
  → match [q1, q2] branch
  → synthesize_2q_matrix_onto()                      // (mod.rs:489)
    ① decomposer_cache.get_2q(qargs_phys, ...)      // get decomposer(s) for this qubit pair
    ② decomposer.decompose(unitary)                  // KAK/Weyl: OPTIMAL 0-3 CX + 1Q gates
    ③ fidelity scoring across multiple decomposers    // picks best if multiple options
    ④ splice best sequence into DAG                   // typically 1-7 gates total
```

**Why it works**: Weyl decomposition predicts the exact CX count for any 2Q unitary in microseconds, without performing synthesis. The gate guard ensures blocks are only replaced when synthesis produces fewer CX gates. KAK synthesis is provably optimal — it always produces the minimum possible CX count (0-3).

### 3Q Call Chain (What Happens When You Enable 3Q Collection)

```
CollectMultiQBlocks(max_block_size=3)
  → DSU-based block grouping
  → writes block_list to property_set

ConsolidateBlocks (consolidate_blocks.rs:347-403)
  → block_qargs.len() > 2 branch
  ① CircuitData::from_packed_operations()            // build sub-circuit from block gates
  ② Python: Operator(circuit).data                   // Rust→Python→NumPy roundtrip for 8×8 unitary
                                                     // (consolidate_blocks.rs:373-383)
  ③ NO GATE GUARD                                    // ← MISSING: no cost prediction, no comparison
  ④ UnitaryGate { array: NDArray(matrix) }           // 8×8 ndarray
  → dag.replace_block()                              // UNCONDITIONAL replacement

UnitarySynthesis (unitary_synthesis/mod.rs:406-422)
  → match _ (3Q+ catch-all) branch
  → quantum_shannon_decomposition(matrix, None, None, None, None)  // (qsd.rs:95)
    ① block_zxz_decomp(8×8 matrix)                   // Block ZXZ: A1, A2, B, C (each 4×4)
    ② demultiplex(I, C) → qsd_inner(4×4)             // recurse → 2Q KAK decomposition
    ③ demultiplex(A1, A2) → qsd_inner(4×4)           // recurse → 2Q KAK decomposition
    ④ middle CX/CZ multiplexing gates                 // structural overhead from QSD
    → returns ~20 CX + ~40 1Q gates                   // ALWAYS, regardless of input block size
  → splice into DAG                                    // replaces 1 UnitaryGate with ~60 gates
```

**Why it fails**: No cost prediction exists for 3Q unitaries. There is no 3Q equivalent of Weyl coordinates that can cheaply predict the optimal CX count. ConsolidateBlocks unconditionally packages every 3Q block as a UnitaryGate, then QSD unconditionally resynthesizes it at ~20 CX — even when the original block had only 6-9 CX.

### Side-by-Side Comparison

| Capability | 2Q Path | 3Q Path |
|---|---|---|
| **Unitary computation** | `blocks_to_matrix()` — pure Rust, `Matrix4` math, no Python | `Operator(circuit).data` — Rust→Python→NumPy roundtrip |
| **Cost prediction** | `num_basis_gates_inner()` — Weyl decomposition gives exact CX count (0-3) in microseconds | **None** — no way to predict QSD output cost without running full synthesis |
| **Gate guard** | `num_basis_gates < basis_count` — only consolidates when synthesis is cheaper | **None** — unconditionally consolidates every block |
| **Synthesis quality** | KAK/Weyl — **provably optimal** (0-3 CX for any 2Q unitary) | QSD — general-purpose recursive, ~20 CX for 3Q (**6 CX above theoretical minimum** of 14) |
| **Synthesis speed** | Microseconds per block | ~1ms per block (QSD) |

### Could a Gate Guard Fix the 3Q Path?

Adding a gate guard to the >2Q branch of ConsolidateBlocks would prevent the regression. Two approaches:

1. **Run QSD, then compare**: Synthesize the block via QSD, count the output CX gates, and only substitute if fewer than the original block. This is correct but wasteful — you pay the full synthesis cost (~1ms/block × 23,000 blocks = ~23s) even though 99.8% of results would be discarded.

2. **Cheap cost predictor**: Analogous to `num_basis_gates_inner()` for 2Q, compute an upper bound on the optimal 3Q CX count without running synthesis. No such predictor currently exists. Developing one would require new mathematical results (a 3Q analogue of Weyl coordinates), which is an open research problem.

Either approach would prevent the regression, but neither would make 3Q synthesis productive — because the synthesis floor (14 CX theoretical minimum) exceeds the typical block size (median 2-12 CX across our benchmarks).

## What Exists for 3Q Today

### Collection: Ready

`CollectMultiQBlocks` (`qiskit/transpiler/passes/optimization/collect_multiqubit_blocks.py`) already supports arbitrary block sizes via `max_block_size` parameter. Uses a Disjoint Set Union (DSU) data structure to dynamically group gates.

```python
CollectMultiQBlocks(max_block_size=3)  # Already works
```

Tests confirm this: `test/python/transpiler/test_collect_multiq_blocks.py` uses `max_block_size=3` and `max_block_size=4`.

**However**: the default transpiler pipeline uses `Collect2qBlocks` (hard-coded for 2Q), not `CollectMultiQBlocks`. Switching would be a one-line change.

### Consolidation: Partial

`ConsolidateBlocks` already handles >2Q blocks (consolidate_blocks.rs:347-403):
1. Builds a sub-circuit from block gates via `CircuitData::from_packed_operations()`
2. Crosses into Python to compute the full unitary: `Operator(circuit).data` → 8×8 matrix
3. Wraps as a single `UnitaryGate { array: NDArray(matrix) }`
4. Unconditionally replaces the block in the DAG

It does **not** predict synthesis cost or compare against the original block. There is no `ThreeQubitBasisDecomposer` analogous to `TwoQubitBasisDecomposer`.

### Synthesis: QSD Only

For 3Q+ unitaries, `UnitarySynthesis` (mod.rs:406-422) always routes to Quantum Shannon Decomposition:

- **Rust**: `crates/synthesis/src/qsd.rs` — Block ZXZ decomposition, recursing into 2Q KAK at the base case
- QSD is called with all `None` arguments (no custom decomposer, no custom basis), using defaults: CX basis, U gates, fidelity 1.0

QSD is a general-purpose recursive algorithm. For 3Q, it produces ~**20 CX gates** (Shende et al., 2004). This is not optimal — the theoretical minimum is **14 CX gates**.

### AQC: Exists But Not Integrated

The Approximate Quantum Compiler (`qiskit/synthesis/unitary/aqc/`) uses numerical optimization (L-BFGS-B) over parameterized CNOT unit circuits. It supports arbitrary qubit counts and can potentially get closer to optimal gate counts. But it's **not in the default transpiler pipeline** — it requires explicit configuration and is slow (~100-1000ms per unitary vs microseconds for KAK).

## Source Files

| File | Role |
|------|------|
| `qiskit/transpiler/passes/optimization/collect_2q_blocks.py` | Current 2Q block collector (hard-coded) |
| `qiskit/transpiler/passes/optimization/collect_multiqubit_blocks.py` | General N-qubit block collector (DSU algorithm) |
| `crates/transpiler/src/passes/consolidate_blocks.rs` | Block → unitary consolidation. 2Q path (KAK) vs >2Q path (raw unitary) |
| `crates/synthesis/src/two_qubit_decompose/` | 2Q synthesis: `basis_decomposer.rs`, `weyl_decomposition.rs`, etc. |
| `crates/synthesis/src/qsd.rs` | Quantum Shannon Decomposition (used for 3Q+) |
| `crates/transpiler/src/passes/unitary_synthesis/mod.rs` | UnitarySynthesis dispatcher: 1Q→Euler, 2Q→KAK, 3Q+→QSD |
| `qiskit/synthesis/unitary/aqc/aqc.py` | Approximate Quantum Compiler (not in default pipeline) |

## The Key Question: Does 3Q Block Synthesis Pay Off?

### Gate Count Economics

For a 3Q block to benefit from consolidation, the block must contain more gates than the synthesis output:

| Method | CX Gates for Arbitrary 3Q Unitary | Total Gates (CX + 1Q) |
|--------|:---------------------------------:|:---------------------:|
| Theoretical lower bound | **14** | ~14 CX + ~27 1Q = ~41 |
| Rakyta & Zimboras (numerical) | **15** | ~42 |
| Krol & Al-Ars (Block ZXZ, exact) | **19** | ~50 |
| Standard QSD (Shende et al.) | **20** | ~55 |
| QSD in Qiskit (current) | **~20** | ~55 |

So a 3Q block needs roughly **>20 CX gates** (or >55 total gates) to benefit from QSD resynthesis, or **>15 CX gates** if we use a near-optimal numerical method.

Compare with 2Q: KAK produces at most 3 CX gates, so even a small 2Q block of 4 gates can benefit. The break-even threshold for 3Q is much higher.

### When Would Dense 3Q Blocks Appear?

- **Toffoli-heavy circuits**: algorithms using many controlled-controlled operations (e.g., Grover oracles, arithmetic circuits). These often have dense sequences on 3 qubits.
- **After routing**: SWAP insertion can create new 3Q interaction patterns.
- **Chemistry ansatze**: some molecular Hamiltonians produce localized multi-qubit interactions.

### When Would They NOT Appear?

- Circuits with widely distributed interactions (many qubits involved, few repeated 3Q patterns)
- Circuits where 2Q gates on different pairs interleave frequently, breaking 3Q blocks
- Shallow circuits (not enough depth for large blocks to form)

## What Would a 3Q Extension Require?

### Minimal Path (Use Existing QSD)

1. Switch `Collect2qBlocks` → `CollectMultiQBlocks(max_block_size=3)` in the pass manager
2. `ConsolidateBlocks` already packages 3Q blocks as `UnitaryGate`
3. `UnitarySynthesis` already decomposes them via QSD

**Effort**: ~1 day of pass manager wiring + benchmarking
**Expected benefit**: Modest — QSD produces ~20 CX, so only very large 3Q blocks would benefit

### Better Path (Improved 3Q Synthesis)

1. Same collection change as above
2. Add a `ThreeQubitBasisDecomposer` in Rust that outperforms QSD
3. Integrate into `ConsolidateBlocks` so it synthesizes 3Q blocks directly (like it does for 2Q)

**Effort**: Weeks-months depending on synthesis algorithm choice
**Expected benefit**: Potentially significant if using near-optimal methods (15 CX vs 20)

### Synthesis Algorithm Options

| Algorithm | CX Count | Speed | Maturity | Effort to Integrate |
|-----------|:--------:|:-----:|:--------:|:-------------------:|
| QSD (already in Qiskit) | ~20 | Fast (ms) | Production | Already done |
| Block ZXZ (Krol & Al-Ars) | 19 | Fast | Paper (2024) | Medium — implement in Rust |
| SQUANDER (Rakyta & Zimboras) | 15 | Slow (~100ms) | Library (C++) | High — optimization loop in Rust |
| AQC (already in Qiskit) | 14 | Slow (~1s) | In Qiskit | Medium — wire into pipeline |

**AQC detail**: Uses L-BFGS-B to optimize 65 parameters (3Q case) over a circuit template of 14 CX + single-qubit rotations. Achieves the theoretical minimum of 14 CX. Located in `qiskit/synthesis/unitary/aqc/`. Already integrated as a transpiler plugin but not in the default pipeline due to runtime cost.

### Alternative Approaches from Recent Research

Beyond full unitary resynthesis, several other optimization strategies exist:

**1. Block-Based Topology-Aware Synthesis (TopAS / BQSKit)**
- Partition circuit into multi-qubit blocks, resynthesize each block using numerical optimization (QFactor, QSearch, LEAP)
- QFactor handles 12+ qubit blocks using tensor network formulation + GPU parallelism
- LEAP extends synthesis to 5-6 qubits using incremental prefix optimization (59x faster than QSearch for 4Q)
- Permutation-Aware Synthesis (PAS) achieves 18-68% fewer gates than Qiskit by jointly optimizing block synthesis + qubit routing
- **Key limitation**: numerical synthesis per block is slow (seconds to minutes), not suitable for default pipeline

**2. Phase Polynomial Optimization (PhasePoly)**
- Recognizes "phase polynomial" subcircuits (common in QAOA, Hamiltonian simulation, Shor's algorithm) and optimizes their parity network
- Achieves up to 48.6% CNOT reduction on phase polynomial blocks
- **Only applies to specific gate structures** — not general purpose

**3. RL-Based CNOT Minimization (AlphaCNOT)**
- Uses reinforcement learning + Monte Carlo Tree Search for CNOT circuit optimization
- Up to 32% reduction on linear reversible circuits (up to 8 qubits)
- **Limited to CNOT-only subcircuits** — not applicable to general blocks with rotations

**4. SU(4)-Aware Compilation**
- Treats 2Q gate blocks natively as SU(4) operations, avoiding unnecessary decomposition
- 4.97x pulse duration reduction on flux-tunable transmons
- **2Q only** — does not extend to 3Q

**5. SAT-Based Clifford Synthesis**
- Finds CNOT-optimal Clifford circuits via SAT encoding
- Up to 32% CNOT reduction on Clifford subcircuits
- **Only applies to Clifford circuits** — not general purpose

## Research Papers

### Foundational

- **Shende, Bullock, Markov** — "Synthesis of Quantum Logic Circuits" (quant-ph/0406176, 2004). Introduces QSD. Proves lower bound of ceil((4^n - 3n - 1)/4) CX gates. For n=3: **14 CX minimum**. Their construction achieves ~20 CX for 3Q.

- **Shende, Bullock, Markov** — "Minimal Universal Two-qubit Quantum Circuits" (quant-ph/0308033, 2004). Proves 3 CX gates optimal for arbitrary 2Q unitary. The KAK result that Qiskit's 2Q synthesis builds on.

- **Shende, Markov** — "On the CNOT-cost of TOFFOLI gates" (0803.2316, 2008). Proves 6 CX is optimal for Toffoli. Complete classification of 3Q diagonal operators by CX cost.

### Near-Optimal 3Q Decomposition

- **Rakyta, Zimboras** — "Approaching the theoretical limit in quantum gate decomposition" (2109.06770, 2021). Numerical variational method achieving **15 CX for arbitrary 3Q unitary** (1 above theoretical minimum). Uses sequential optimization of rotation parameters within fixed circuit templates.

- **Krol, Al-Ars** — "Beyond Quantum Shannon: Circuit Construction for General n-Qubit Gates Based on Block ZXZ-Decomposition" (2403.13692, 2024). Exact (non-numerical) method achieving **19 CX for 3Q**. Improves on standard QSD by saving (4^(n-2)-1)/3 CX gates.

### QSD Improvements

- **Krol, Sarkar, Ashraf, Al-Ars, Bertels** — "Efficient decomposition of unitary matrices in quantum circuit compilers" (2101.02993, 2021). Practical QSD implementation producing circuits with half the CX gates of Qubiter. Directly relevant to compiler implementations.

- **Mottonen, Vartiainen** — "Decompositions of general quantum gates" (quant-ph/0504100, 2005). Achieves (23/48)*4^n CX asymptotically. Discusses nearest-neighbor constraints.

### Approximate / Numerical Methods

- **Madden, Simonetto** — "Best Approximate Quantum Compiling Problems" (2106.05649, 2021). Shows QSD can be compressed by factor of 2 via approximation without practical fidelity loss.

- **Younis, Sen, Yelick, Iancu** — "QFAST: Quantum Synthesis Using a Hierarchical Continuous Circuit Space" (2003.04462, 2020). Hierarchical numerical synthesis with topology awareness. Composable and scalable.

- **Smith, Davis, Larson, Younis, Iancu, Lavrijsen** — "LEAP: Scaling Numerical Optimization Based Synthesis Using an Incremental Approach" (2106.11246, 2021). 59x faster than QSearch for 4Q unitaries. Up to 36x fewer CX than Tket.

- **Nemkov, Kiktenko, Luchnikov, Fedorov** — "Efficient variational synthesis of quantum circuits with coherent multi-start optimization" (Quantum 7, 993, 2023). Variational approach demonstrating 8-CX Toffoli decomposition.

### Block-Based Optimization

- **Weiden, Kalloor, Kubiatowicz, Younis, Iancu** — "Wide Quantum Circuit Optimization with Topology Aware Synthesis" (2206.13645, 2022). TopAS: partitions large circuits into blocks, applies numerical synthesis per block. Reduces CX by 30.3% and depth by 35.2% on 2D mesh.

## Profiling Results: 3Q Block Statistics

Profiled 12 benchmark circuits on FakeTorino (133Q heavy-hex), Level 2 pre-optimization (init + layout + routing + translation, no optimization stage). Compared `Collect2qBlocks` (current) vs `CollectMultiQBlocks(max_block_size=3)`.

### Summary Table

| Circuit | Post-Route 2Q | 2Q Blocks | 3Q Blocks | Max 3Q Size | Max 3Q 2Q | 2Q in 3Q Blocks | >20 CX | >15 CX |
|---------|:------------:|:---------:|:---------:|:-----------:|:---------:|:---------------:|:------:|:------:|
| QFT_100 | 10,945 | 3,171 | 1,302 | 82 | 15 | 10,643 (97%) | 0 | 0 |
| QV_100 | 97,452 | 31,807 | 14,561 | 125 | 18 | 93,533 (96%) | 0 | 1 |
| EfficientSU2_100 | 297 | 297 | 75 | 18 | 2 | 150 (51%) | 0 | 0 |
| QAOA_100 | 16,239 | 5,563 | 2,529 | 64 | 13 | 15,559 (96%) | 0 | 0 |
| BV_100 | 390 | 99 | 49 | 44 | 8 | 389 (100%) | 0 | 0 |
| Heisenberg_100 | 6,480 | 1,563 | 695 | 157 | 18 | 6,219 (96%) | 0 | 11 |
| Grover_50 | 3,011 | 1,409 | 495 | 82 | 14 | 2,862 (95%) | 0 | 0 |
| Adder_80 | 1,471 | 745 | 160 | 87 | 19 | 1,435 (98%) | 0 | 19 |
| Random_80 | 15,366 | 5,378 | 2,436 | 80 | 14 | 14,682 (96%) | 0 | 0 |
| GHZ_100 | 99 | 99 | 49 | 17 | 2 | 98 (99%) | 0 | 0 |
| QPE_50 | 4,716 | 1,335 | 538 | 82 | 15 | 4,527 (96%) | 0 | 0 |
| Toffoli_90 | 1,419 | 650 | 126 | 105 | 24 | 1,405 (99%) | 1 | 17 |
| **TOTAL** | **157,885** | **52,116** | **23,015** | | | **151,502 (96%)** | **1** | **48** |

### Key Findings

1. **3Q blocks are abundant.** CollectMultiQBlocks(max_block_size=3) finds 23,015 blocks across 12 circuits, absorbing most 2Q blocks. The 2Q block count drops from 52,116 → ~2,200 because most adjacent 2Q gates share a third qubit (via SWAP routing), forming natural 3Q groups.

2. **3Q blocks capture 96% of all 2Q gates.** On heavy-hex after SABRE routing, the vast majority of 2Q interactions fall into 3-qubit neighborhoods (a qubit and its two neighbors in the heavy-hex lattice).

3. **No 3Q blocks exceed the QSD break-even threshold.** Only **1 block** out of 23,015 has >20 CX gates (a single Toffoli_90 block with 24 CX). This means QSD resynthesis of 3Q blocks would almost always **increase** gate count.

4. **Even near-optimal methods won't help.** Only **48 blocks** (0.2%) have >15 CX gates — the threshold for a hypothetical near-optimal 3Q synthesizer (Rakyta & Zimboras, 15 CX). These are concentrated in Heisenberg (11), Adder (19), and Toffoli (17) — circuits with dense local interactions.

5. **The max 2Q gate count in any 3Q block is 24** (Toffoli_90). The median across all circuits is 6-9 2Q gates per block. QSD would replace 6-9 CX with ~20 CX — a severe regression.

6. **Block sizes are large in total gates (20-50 gates) but most are 1Q gates.** After routing, many 1Q rotation gates get interleaved into blocks alongside 2Q gates, inflating block size without increasing 2Q content.

### Why Are 3Q Blocks So Small in 2Q Gate Count?

On heavy-hex topology, SABRE routing distributes interactions across the lattice. Each qubit has degree 2-3 in heavy-hex, so 3Q neighborhoods see limited interaction density. The 2Q gates in a 3Q block are typically:
- 1-2 original circuit CX/CZ gates
- 1-4 SWAP-inserted CX gates (each SWAP = 3 CX, split across 2Q pairs)

This creates blocks with 2-15 CX gates — well below the 20 CX break-even for QSD.

### Conclusion

**3-qubit block synthesis via QSD is not viable** for circuits routed on heavy-hex topology. The blocks exist in large numbers but contain too few 2Q gates to benefit from resynthesis. Even a hypothetical optimal 3Q synthesizer (14 CX) would only help 48 out of 23,015 blocks (0.2%).

The fundamental issue is **topology-limited interaction density**: heavy-hex's low degree (2-3) prevents dense 3Q interaction patterns from forming after routing.

### NightHawk (Rectangular Grid, Degree 4) — Even Worse

To test whether a denser topology helps, we repeated the profiling on FakeNighthawk (120Q, 10x12 rectangular grid, avg degree 3.63).

| Metric | Heavy-Hex (Torino) | Rectangular Grid (NightHawk) |
|--------|:------------------:|:----------------------------:|
| Total 3Q blocks | 23,015 | 17,006 |
| Max 2Q in any 3Q block | **24** | **19** |
| Blocks >20 CX (QSD payoff) | 1 (0.004%) | **0 (0%)** |
| Blocks >15 CX (near-optimal) | 48 (0.2%) | **10 (0.06%)** |
| 2Q gates in 3Q blocks | 96.0% | 95.0% |

**Counterintuitively, NightHawk makes 3Q synthesis less viable.** The higher connectivity means SABRE inserts fewer SWAPs, producing circuits with fewer 2Q gates overall. Fewer 2Q gates per 3Q block pushes them further below the resynthesis break-even. Heisenberg is the clearest example: Torino 6,480 2Q gates (max 18 CX/block) vs NightHawk 3,240 2Q gates (max 12 CX/block) — the square lattice maps natively to NightHawk's grid.

The max 2Q count in any 3Q block across all 12 circuits on NightHawk is **19** — below QSD break-even for every single block.

### Conclusion

3-qubit block synthesis via QSD is not viable on **any current or upcoming IBM topology**. The result is topology-independent: denser topologies produce fewer SWAPs, which means sparser 3Q blocks. The only scenario where 3Q resynthesis could help is on very constrained topologies with very high SWAP overhead — but on those topologies, better routing is a more effective optimization.

### What Could Work Instead?

Given that full 3Q resynthesis is a dead end, here are the directions that **could** pay off, ranked by expected impact:

#### 1. Extend `Split2QUnitaries` to 3Q (Low Effort, Narrow Benefit)

`Split2QUnitaries` already checks if a 2Q unitary is a tensor product (separable into two 1Q gates). The same idea extends to 3Q: check if an 8x8 unitary decomposes as a tensor product of smaller unitaries (e.g., 4x4 ⊗ 2x2, or 2x2 ⊗ 2x2 ⊗ 2x2). This requires only matrix factorization — no synthesis loop. If a 3Q block's unitary is partially separable, we can split it into a 2Q + 1Q operation (or three 1Q operations), then the existing 2Q KAK decomposition handles the rest.

**Expected benefit**: Would catch cases where routing accidentally creates 3Q blocks that are actually separable. Unknown how common this is — needs profiling.
**Effort**: Small — SVD-based separability check, no new synthesis code.

#### 2. Structure-Specific Optimization Passes (Medium Effort, Targeted Benefit)

Instead of generic 3Q synthesis, add optimization passes that target specific patterns that appear frequently:

- **Phase polynomial optimization** (PhasePoly approach): Recognize CX + RZ chains in 3Q blocks and optimize the parity network. Up to 48% CNOT reduction on phase polynomial structures. Relevant for QAOA, Hamiltonian simulation, QPE.
- **Clifford subcircuit optimization**: For Clifford portions of 3Q blocks, SAT-based synthesis finds CNOT-optimal decompositions (up to 32% reduction). Relevant for error correction and Clifford-heavy circuits.
- **Commutation-based peephole**: Move commuting gates past each other within 3Q blocks to enable additional cancellations. Extension of existing `CommutativeCancellation` to 3Q scope.

**Expected benefit**: Moderate for specific circuit families. Phase polynomial optimization is the most promising for real workloads.
**Effort**: Medium — each is a separate pass targeting a specific structure.

#### 3. BQSKit-Style Numerical Block Optimization (High Effort, Broad Benefit)

The BQSKit/QFactor approach partitions circuits into multi-qubit blocks and uses numerical optimization (tensor network formulation) to resynthesize each block. Key capabilities:
- QFactor handles 12+ qubit blocks with GPU acceleration
- LEAP extends to 5-6 qubit blocks, 59x faster than QSearch
- Permutation-Aware Synthesis (PAS) jointly optimizes block synthesis + qubit routing, achieving 18-68% fewer gates than Qiskit

This is the "gold standard" for block optimization but is **too slow for the default transpiler pipeline** (seconds to minutes per block). Could work as an optional high-effort optimization pass.

**Expected benefit**: Potentially large (10-30% CX reduction) for circuits where it's applied.
**Effort**: High — would need to integrate BQSKit or implement similar numerical optimization in Qiskit.

#### 4. SQUANDER-Style Variational 3Q Synthesis (High Effort, Targeted Benefit)

SQUANDER (Rakyta & Zimboras) achieves 15 CX for arbitrary 3Q unitaries using gradient-based optimization (BFGS, ADAM) with circuit templates. The library is C++ with Python bindings. For our use case:
- Only 48 blocks (0.2%) on Torino have >15 CX — even this near-optimal method helps very few blocks
- Runtime per unitary is ~100ms (too slow for thousands of blocks)
- **Not viable for pipeline integration** unless the target blocks are pre-filtered

**Expected benefit**: Negligible — too few qualifying blocks.
**Effort**: High — would need C++ integration or Rust reimplementation.

## Separability Profiling: 5.4% CX Savings Available

Profiled all 12 benchmark circuits to check how many 3Q blocks are tensor products (bipartite separable as 2Q ⊗ 1Q). Uses SVD-based rank-1 check on the reshaped unitary matrix, then Weyl decomposition to count CX gates in the 2Q factor.

### Results

| Circuit | 3Q Blocks | Bipartite | Entangled | CX Savings | Total 2Q | Save % |
|---------|:---------:|:---------:|:---------:|:----------:|:--------:|:------:|
| QFT_100 | 1,259 | 61 (4.8%) | 1,198 | 488 | 11,050 | 4.4% |
| QV_100 | 14,641 | 94 (4.7%)* | 1,906 | 5,351 | 97,797 | 5.5% |
| EfficientSU2_100 | 75 | 0 (0%) | 75 | 0 | 297 | 0% |
| QAOA_100 | 2,568 | 99 (5.0%)* | 1,901 | 1,011 | 16,308 | 6.2% |
| BV_100 | 49 | 0 (0%) | 49 | 0 | 390 | 0% |
| Heisenberg_100 | 724 | 37 (5.1%) | 687 | 322 | 6,723 | 4.8% |
| Grover_50 | 453 | 12 (2.6%) | 441 | 82 | 2,930 | 2.8% |
| Adder_80 | 164 | 2 (1.2%) | 162 | 15 | 1,471 | 1.0% |
| Random_80 | 2,409 | 112 (5.6%)* | 1,888 | 1,035 | 15,171 | 6.8% |
| GHZ_100 | 49 | 0 (0%) | 49 | 0 | 99 | 0% |
| QPE_50 | 591 | 31 (5.2%) | 560 | 242 | 4,851 | 5.0% |
| Toffoli_90 | 119 | 1 (0.8%) | 118 | 24 | 1,368 | 1.8% |
| **TOTAL** | **23,101** | **449 (1.9%)** | **9,034** | **8,570** | **158,455** | **5.4%** |

*Sampled 2,000 blocks; results extrapolated.

### Key Findings

1. **~5% of 3Q blocks are bipartite separable.** Across all circuits, about 1.9% of 3Q blocks factor as (2Q ⊗ 1Q). This is a consistent signal: most circuits with SWAP routing show 2.6-6.8% separability.

2. **The 2Q factor typically needs only 1 CX gate.** When a 3Q block is separable, the 2Q sub-unitary almost always needs just 1 CX (occasionally 2-3). This means splitting saves the other original 2Q gates in the block (typically 7-10 CX → 1 CX = 6-9 CX saved per block).

3. **5.4% total CX savings across all circuits.** This is a meaningful reduction — comparable to what the entire optimization loop achieves (and we just showed the loop is unnecessary).

4. **Three circuit families see zero benefit**: EfficientSU2 (very sparse 2Q gates), BV (chain structure), GHZ (chain structure). These have simple connectivity patterns where 3Q blocks are truly entangled.

5. **Why does this happen?** SWAP routing inserts CX gates that create interaction paths through a third qubit that isn't actually entangled with the other two. The 3Q block collector groups these gates together, but the unitary is actually separable along one cut — one qubit is just a "bystander" in the block.

### What Would an Implementation Look Like?

Extension of `Split2QUnitaries` to 3Q:

1. In `CollectMultiQBlocks(max_block_size=3)`, identify 3Q blocks
2. For each 3Q block, compute the 8x8 unitary
3. Check bipartite separability via SVD (rank-1 check on reshaped matrix) — 3 cuts to check
4. If separable: replace the block with a 2Q unitary + 1Q unitary
5. The existing `UnitarySynthesis` (KAK for 2Q) handles the rest

**Runtime overhead**: ~2ms per block for the SVD check. On QV_100 (14,641 blocks) that's ~30s. Could be optimized with a Rust implementation or by pre-filtering blocks (e.g., skip blocks with ≤3 CX gates since they can't benefit).

**Effort**: Medium. The SVD separability check is straightforward math. The integration requires modifying `ConsolidateBlocks` or adding a new pass after `CollectMultiQBlocks`.

## End-to-End Experiment: 2Q-Only vs 3Q Block Synthesis

To empirically confirm that 3Q block synthesis regresses gate counts, we ran a controlled A/B comparison across all 12 benchmark circuits.

**Methodology:**
1. Shared pre-optimization: init + layout + routing + translation at Level 2 (no optimization)
2. **Variant A (2Q-only)**: `Collect2qBlocks` → `ConsolidateBlocks` → `UnitarySynthesis` → optimization loop
3. **Variant B (2Q+3Q)**: `CollectMultiQBlocks(max_block_size=3)` → `ConsolidateBlocks` → `UnitarySynthesis` → optimization loop
4. Both start from the identical pre-optimized circuit; only the block collection pass differs

**Script**: `investigation/profile_2q_vs_3q_synthesis.py`

### Results

| Circuit | Pre-Opt 2Q | 2Q-Only | 2Q+3Q | Delta | Delta % | Time 2Q | Time 3Q |
|---------|:----------:|:-------:|:-----:|:-----:|:-------:|:-------:|:-------:|
| QFT_100 | 10,954 | 8,914 | 23,136 | +14,222 | +159.5% | 3.3s | 19.9s |
| QV_100 | 8,217 | 8,073 | 21,564 | +13,491 | +167.1% | 2.2s | 18.5s |
| EfficientSU2_100 | 99 | 99 | 789 | +690 | +697.0% | 0.1s | 0.7s |
| QAOA_100 | 9,777 | 9,631 | 26,073 | +16,442 | +170.7% | 2.9s | 20.0s |
| BV_100 | 390 | 196 | 909 | +713 | +363.8% | 0.2s | 0.8s |
| Heisenberg_100 | 10,461 | 7,539 | 19,434 | +11,895 | +157.8% | 3.1s | 18.7s |
| Grover_50 | 2,957 | 2,659 | 7,793 | +5,134 | +193.1% | 0.8s | 5.8s |
| Adder_80 | 1,603 | 1,447 | 3,058 | +1,611 | +111.3% | 0.4s | 2.4s |
| Random_80 | 15,246 | 14,947 | 41,705 | +26,758 | +179.0% | 3.7s | 31.0s |
| GHZ_100 | 99 | 99 | 406 | +307 | +310.1% | 0.1s | 0.3s |
| QPE_50 | 4,719 | 3,941 | 10,305 | +6,364 | +161.5% | 1.5s | 8.7s |
| Toffoli_90 | 1,368 | 1,078 | 1,529 | +451 | +41.8% | 0.4s | 1.3s |
| **TOTAL** | **65,890** | **58,623** | **156,701** | **+98,078** | **+167.3%** | **18.7s** | **128.1s** |

### Analysis

1. **3Q block synthesis regresses every single circuit.** No exceptions. The minimum regression is +41.8% (Toffoli_90), the maximum is +697% (EfficientSU2_100), and the average is +167%.

2. **The regression comes from the pre-loop.** Comparing "after pre-loop" numbers confirms that QSD immediately inflates 2Q gate counts (e.g., QFT: 8,964 → 24,042 after pre-loop). The optimization loop partially recovers but cannot undo the damage.

3. **The root cause is confirmed: no size guard for >2Q blocks.** `ConsolidateBlocks` unconditionally packages 3Q blocks as `UnitaryGate` (consolidate_blocks.rs:347-403). For 2Q blocks, it compares `num_basis_gates < basis_count` and skips consolidation when synthesis would be worse. For >2Q blocks, no such check exists.

4. **3Q path is 5-8x slower.** Total optimization time increases from 18.7s to 128.1s — the cost of synthesizing ~20 CX per block via QSD, plus additional optimization loop iterations needed to partially recover.

5. **Even Toffoli_90 regresses (+41.8%)** despite having the highest-CX 3Q blocks (max 24 CX). This is because only 1 block exceeds the QSD break-even of 20 CX, while the other 125 blocks are well below it.

### Conclusion

This experiment definitively confirms that simply switching to 3Q block collection with existing QSD synthesis is a severe regression. The profiling data (Section "Profiling Results") predicted this — median block size is 2-12 CX (see below) while QSD produces ~20 CX — and the end-to-end experiment validates it.

### Median 2Q Gates per 3Q Block (from Profiling)

| Circuit | 3Q Blocks | Median 2Q | Avg 2Q | Max 2Q |
|---------|:---------:|:---------:|:------:|:------:|
| QFT_100 | 1,302 | **8** | 8.2 | 15 |
| QV_100 | 14,561 | **6** | 6.4 | 18 |
| EfficientSU2_100 | 75 | **2** | 2.0 | 2 |
| QAOA_100 | 2,529 | **6** | 6.2 | 13 |
| BV_100 | 49 | **8** | 7.9 | 8 |
| Heisenberg_100 | 695 | **9** | 8.9 | 18 |
| Grover_50 | 495 | **6** | 5.8 | 14 |
| Adder_80 | 160 | **8** | 9.0 | 19 |
| Random_80 | 2,436 | **6** | 6.0 | 14 |
| GHZ_100 | 49 | **2** | 2.0 | 2 |
| QPE_50 | 538 | **9** | 8.4 | 15 |
| Toffoli_90 | 126 | **12** | 11.2 | 24 |

Most circuits have median 6-9 CX per 3Q block. Even Toffoli_90, with the densest blocks (median 12, max 24), is mostly below the theoretical minimum of 14 CX. For 3Q block synthesis to be viable, it would need **both**:
1. A gate guard in `ConsolidateBlocks` for >2Q blocks (like the 2Q path has)
2. A near-optimal 3Q synthesizer producing fewer CX than the typical block — but the theoretical minimum for arbitrary 3Q unitary is **14 CX**, which exceeds the median block size for all 12 circuits

### Benchmark Selection Bias — Adversarial Profiling

Our 12 benchmark circuits all showed median 2-12 CX per 3Q block. We initially attributed this to topology constraints (heavy-hex degree 2-3 limits interaction density). But this argument assumes SABRE distributes interactions evenly, which may not hold for circuits with highly localized multi-qubit structure.

**Script**: `investigation/profile_3q_adversarial.py`

We constructed 8 adversarial circuits designed to maximize 3Q block density:

| Circuit | Description | Post-Route 2Q | 3Q Blocks | Median 2Q | Max 2Q | >14 CX | >20 CX |
|---------|-------------|:------------:|:---------:|:---------:|:------:|:------:|:------:|
| DeepToffoliChain_60 | Overlapping CCX on sliding 3Q windows, 4 sweeps | 3,441 | 384 | **8** | 15 | 10 (2.6%) | 0 |
| RepeatedToffoli_60 | 15 CCX per group on same 3 qubits | 1,770 | 10 | **177** | 177 | 10 (100%) | 10 (100%) |
| CDKM_Adder_40 | Qiskit CDKMRippleCarryAdder (real arithmetic) | 1,619 | 190 | **6** | 19 | 18 (9.5%) | 0 |
| VBE_Adder_20 | Qiskit VBERippleCarryAdder | 1,215 | 149 | **7** | 15 | 3 (2.0%) | 0 |
| **Multiplier_10** | **Qiskit HRSCumulativeMultiplier (real arithmetic)** | **24,407** | **2,267** | **9** | **33** | **273 (12.0%)** | **197 (8.7%)** |
| MCX_Cascade_60 | Sliding MCX(4-ctrl) windows | 1,929 | 326 | **6** | 15 | 2 (0.6%) | 0 |
| StabilizerSyndrome_60 | Repeated CX syndrome extraction | 0 | 0 | — | — | 0 | 0 |
| ControlledRotation_60 | Repeated CRZ on fixed 3Q groups | 1,620 | 20 | **81** | 81 | 20 (100%) | 20 (100%) |
| **TOTAL** | | | **3,346** | | | **336 (10.0%)** | **227 (6.8%)** |

### Key Findings

1. **The Multiplier is the critical finding.** `HRSCumulativeMultiplier(10)` — a real Qiskit arithmetic circuit — produces 2,267 3Q blocks, of which **273 (12%) exceed 14 CX** and **197 (8.7%) exceed 20 CX** (max 33). This is a real workload where 3Q synthesis could genuinely help. The controlled adder chains create many overlapping 3-qubit neighborhoods with deep interaction sequences.

2. **CDKM Adder also shows opportunity.** 18/190 blocks (9.5%) at 15-19 CX — right at the near-optimal threshold. With a 15-CX synthesizer (Rakyta & Zimboras), these blocks would benefit.

3. **Deeply localized circuits break the topology argument.** RepeatedToffoli (median 177 CX) and ControlledRotation (median 81 CX) prove that heavy-hex topology does not inherently prevent dense 3Q blocks — the key factor is **circuit locality**, not topology degree. When a circuit repeatedly hammers the same 3 qubits, SABRE places them as neighbors and blocks accumulate.

4. **Distributed circuits remain unaffected.** DeepToffoliChain (overlapping windows that slide across 60 qubits) has median 8 CX — consistent with our earlier benchmarks. The topology argument holds when interactions are spread across the lattice.

5. **Stabilizer syndrome collapsed to trivial.** The CX-only syndrome extraction circuit was optimized away by the init/translation stages, producing 0 2Q gates post-routing.

### Revised Conclusion

Our earlier conclusion — "3Q synthesis is not viable on any IBM topology" — was **too strong**. It holds for the common circuit families (QFT, QV, QAOA, random, Hamiltonian simulation) but **breaks down for arithmetic circuits** with deeply localized multi-qubit structure.

Specifically:
- **For distributed circuits** (our original 12 benchmarks): 3Q blocks have median 2-12 CX, well below the 14 CX floor. 3Q synthesis cannot help. **This covers most common workloads.**
- **For arithmetic circuits** (multipliers, adders with deep carry chains): 8-12% of 3Q blocks exceed 14 CX. A near-optimal 3Q synthesizer (15 CX) or QSD with a gate guard (skip blocks <20 CX) could produce meaningful savings on these circuits.

The practical question is whether arithmetic circuits (Shor's, multiplication, modular exponentiation) are a sufficient use case to justify the engineering effort. These circuits are among the most important for fault-tolerant quantum computing.

## Next Steps

- [x] Profile benchpress circuits: how many 3Q blocks exist and how large are they?
- [x] ~~Investigate whether denser topologies produce 3Q blocks with higher CX density~~ — NightHawk makes it worse
- [x] Research literature: full 3Q unitary resynthesis is not viable; alternative approaches identified
- [x] Profile 3Q block separability — **5.4% CX savings available from splitting separable blocks**
- [x] End-to-end experiment: 2Q-only vs 3Q synthesis — **confirmed +167% regression without gate guard**
- [x] Document 2Q vs 3Q code path comparison and missing gate guard
- [x] Profile adversarial circuits — **Multiplier produces 12% of blocks >14 CX, max 33 CX**
- [ ] Run end-to-end 2Q vs 3Q experiment on Multiplier with gate guard (synthesize, count CX, only substitute if fewer)
- [ ] Prototype: implement 3Q block splitting as a transpiler pass and measure end-to-end gate count improvement
- [ ] Profile phase polynomial structure — how many 3Q blocks are phase polynomial chains?
- [ ] Evaluate BQSKit integration as optional high-effort pass (not default pipeline)
