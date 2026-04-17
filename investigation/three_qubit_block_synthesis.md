# Investigation: 3-Qubit Block Collection and Synthesis

## Overview

The Qiskit team suggested exploring 3-qubit block detection and synthesis as a potential optimization direction. Qiskit already has state-of-the-art 2-qubit synthesis (KAK/Weyl decomposition, 0-3 CX gates, microseconds). The question is: **can we extend this to 3-qubit blocks, and does it pay off?**

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

## Current Pipeline: How 2Q Block Synthesis Works

The 2Q block optimization pipeline runs in two places:

1. **Init stage** (before routing): `Collect2qBlocks → Collect1qRuns → ConsolidateBlocks → Split2QUnitaries`
2. **Optimization pre-loop** (after routing): `ConsolidateBlocks → UnitarySynthesis`

### Step 1: Block Collection

**Pass**: `Collect2qBlocks` (`qiskit/transpiler/passes/optimization/collect_2q_blocks.py`)

Calls `dag.collect_2q_runs()` — a Rust bicolor graph algorithm that finds maximal runs of gates acting on ≤2 qubits. Gates must be non-parameterized and unitary. The algorithm groups adjacent gates that share the same qubit pair.

Example: `CX(0,1) → RZ(1) → CX(0,1)` on qubits {0,1} → one block of 3 gates.

### Step 2: Consolidation

**Pass**: `ConsolidateBlocks` (`crates/transpiler/src/passes/consolidate_blocks.rs`)

For each 2Q block:
1. Multiplies gate matrices → single 4×4 unitary
2. Uses KAK/Weyl decomposition to compute optimal CX count (0, 1, 2, or 3)
3. If decomposed gate count < original block size → replaces the block

Decision criteria (line 410-426 of consolidate_blocks.rs):
- `force_consolidate` flag set, OR
- `num_basis_gates < basis_count` (synthesis is more efficient), OR
- block depth > `MAX_2Q_DEPTH` (20), OR
- block contains gates outside the target basis

### Step 3: Synthesis

**Pass**: `UnitarySynthesis` (`crates/transpiler/src/passes/unitary_synthesis/mod.rs`)

Dispatches by qubit count (line 389-423):
- **1Q**: `OneQubitEulerDecomposer` — Euler angle decomposition
- **2Q**: `TwoQubitBasisDecomposer` — KAK/Weyl decomposition (0-3 CX gates)
- **3Q+**: `quantum_shannon_decomposition()` — QSD recursive decomposition

## What Exists for 3Q Today

### Collection: Ready

`CollectMultiQBlocks` (`qiskit/transpiler/passes/optimization/collect_multiqubit_blocks.py`) already supports arbitrary block sizes via `max_block_size` parameter. Uses a Disjoint Set Union (DSU) data structure to dynamically group gates.

```python
CollectMultiQBlocks(max_block_size=3)  # Already works
```

Tests confirm this: `test/python/transpiler/test_collect_multiq_blocks.py` uses `max_block_size=3` and `max_block_size=4`.

**However**: the default transpiler pipeline uses `Collect2qBlocks` (hard-coded for 2Q), not `CollectMultiQBlocks`. Switching would be a one-line change.

### Consolidation: Partial

`ConsolidateBlocks` already handles >2Q blocks (line 347 of consolidate_blocks.rs):
1. Extracts block as N-qubit circuit
2. Computes full unitary matrix via `quantum_info.Operator` (Python call)
3. Wraps as a single `UnitaryGate`

But it does **not** attempt synthesis — it just packages the unitary and leaves it for downstream `UnitarySynthesis` to handle. There is no `ThreeQubitBasisDecomposer` analogous to `TwoQubitBasisDecomposer`.

### Synthesis: QSD Only

For 3Q+ unitaries, `UnitarySynthesis` always routes to Quantum Shannon Decomposition:

- **Rust**: `crates/synthesis/src/qsd.rs` — recursive Block ZXZ decomposition
- **Python fallback**: `qiskit/synthesis/unitary/qsd.py`

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

## Next Steps

- [x] Profile benchpress circuits: how many 3Q blocks exist and how large are they?
- [x] ~~Investigate whether denser topologies produce 3Q blocks with higher CX density~~ — NightHawk makes it worse
- [x] Research literature: full 3Q unitary resynthesis is not viable; alternative approaches identified
- [x] Profile 3Q block separability — **5.4% CX savings available from splitting separable blocks**
- [ ] Prototype: implement 3Q block splitting as a transpiler pass and measure end-to-end gate count improvement
- [ ] Profile phase polynomial structure — how many 3Q blocks are phase polynomial chains?
- [ ] Evaluate BQSKit integration as optional high-effort pass (not default pipeline)
