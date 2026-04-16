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
| Numerical (Rakyta & Zimboras) | 15 | Slow (~100ms) | Paper (2021) | High — optimization loop in Rust |
| AQC (already in Qiskit) | ~15-20 | Slow (~1s) | In Qiskit | Medium — wire into pipeline |

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

## Next Steps

- [ ] Profile benchpress circuits: how many 3Q blocks exist and how large are they?
- [ ] Quick test: swap Collect2qBlocks → CollectMultiQBlocks(max_block_size=3) and measure impact
- [ ] Evaluate QSD output quality on collected 3Q blocks vs original gate count
- [ ] If promising: investigate implementing Krol & Al-Ars (2024) Block ZXZ in Rust for better 3Q synthesis
