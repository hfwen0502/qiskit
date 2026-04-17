# Investigation: 3-Qubit Block Collection and Synthesis

## Overview

The Qiskit team suggested exploring 3-qubit block synthesis as an optimization direction. Qiskit has state-of-the-art 2-qubit synthesis (KAK/Weyl, 0-3 CX, microseconds). The question: **can we extend this to 3-qubit blocks, and does it pay off?**

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

**Bottom line**: 3Q synthesis via QSD regresses gate counts by +167% on standard benchmarks because blocks are too small (median 6-9 CX) relative to QSD output (~20 CX). However, **arithmetic circuits** (multipliers, adders) produce blocks where 8-12% exceed 14 CX — the theoretical minimum for 3Q synthesis. These are the circuits where 3Q synthesis could help, if equipped with a gate guard and a near-optimal synthesizer.

## Code Paths: 2Q vs 3Q Block Synthesis

The optimization pipeline: `Collect*Blocks → ConsolidateBlocks → UnitarySynthesis`. All pieces for 3Q already exist. The critical difference is that the 3Q path lacks cost prediction and a gate guard.

### 2Q Call Chain (Current Pipeline)

```
Collect2qBlocks
  → dag.collect_2q_runs()                          // Rust bicolor graph algorithm

ConsolidateBlocks (consolidate_blocks.rs:404-452)
  → block_qargs.len() == 2 branch
  ① blocks_to_matrix()                              // Pure Rust: Matrix4 multiplication
  ② num_basis_gates_inner(matrix)                    // Weyl coordinates → 0-3 CX, microseconds
  ③ GATE GUARD: if num_basis_gates < basis_count     // ← only consolidates when synthesis is cheaper
  ④ UnitaryGate { array: TwoQ(matrix) }              // wrap as 4×4 unitary

UnitarySynthesis (mod.rs:401-404)
  → synthesize_2q_matrix_onto()
    → KAK/Weyl decomposition                        // PROVABLY OPTIMAL: 0-3 CX
```

### 3Q Call Chain (When 3Q Collection is Enabled)

```
CollectMultiQBlocks(max_block_size=3)
  → DSU-based block grouping

ConsolidateBlocks (consolidate_blocks.rs:347-403)
  → block_qargs.len() > 2 branch
  ① CircuitData::from_packed_operations()            // build sub-circuit
  ② Python: Operator(circuit).data                   // Rust→Python→NumPy for 8×8 unitary
  ③ NO GATE GUARD                                    // ← MISSING: unconditional replacement
  ④ UnitaryGate { array: NDArray(matrix) }

UnitarySynthesis (mod.rs:406-422)
  → quantum_shannon_decomposition()                  // QSD: ~20 CX ALWAYS, regardless of input
```

### What the 3Q Path is Missing

| Capability | 2Q Path | 3Q Path |
|---|---|---|
| **Unitary computation** | Pure Rust `Matrix4` math | Rust→Python→NumPy roundtrip |
| **Cost prediction** | Weyl decomposition: exact CX count in microseconds | **None** |
| **Gate guard** | `num_basis_gates < basis_count` | **None** — unconditional |
| **Synthesis quality** | KAK: provably optimal (0-3 CX) | QSD: ~20 CX (theoretical min: 14) |

### Could a Gate Guard Fix It?

Yes — two approaches:

1. **Run QSD, then compare**: Synthesize via QSD, count output CX, only substitute if fewer. Correct but wasteful (~1ms/block × thousands of blocks, mostly discarded).
2. **Cheap cost predictor**: A 3Q analogue of Weyl coordinates. No such predictor exists — this is an open research problem.

Either prevents the regression. But for most circuits, neither makes 3Q synthesis *productive* — because the synthesis floor (14 CX) exceeds the typical block size (6-9 CX).

## 3Q Synthesis Cost: What Algorithms Exist?

| Algorithm | CX Gates (3Q) | Speed | Status |
|-----------|:-------------:|:-----:|--------|
| Theoretical lower bound | **14** | — | Proven (Shende et al., 2004) |
| SQUANDER (Rakyta & Zimboras) | **15** | ~100ms | C++ library |
| Block ZXZ (Krol & Al-Ars) | **19** | Fast | Paper (2024) |
| QSD in Qiskit | **~20** | ~1ms | Production |
| AQC in Qiskit | **~14** | ~1s | In Qiskit, not in default pipeline |

For 2Q, KAK produces 0-3 CX — so even a 4-CX block benefits. For 3Q, the best methods produce 14-20 CX — so only blocks with >14 CX can benefit.

## Profiling Results

All profiling on FakeTorino (133Q, heavy-hex), Level 2 pre-optimization (init + layout + routing + translation, no optimization stage).

**Scripts**: `investigation/profile_3q_blocks.py`, `investigation/profile_3q_adversarial.py`

### Standard Benchmarks (12 circuits)

`CollectMultiQBlocks(max_block_size=3)` finds 23,015 blocks across 12 circuits (QFT, QV, QAOA, Grover, Heisenberg, Adder, Random, GHZ, QPE, BV, EfficientSU2, Toffoli). These blocks capture **96% of all 2Q gates**.

- **Median CX per block**: 2-12 (most circuits 6-9)
- **Max CX in any block**: 24 (Toffoli_90)
- **Blocks >20 CX** (QSD break-even): 1 / 23,015 (0.004%)
- **Blocks >14 CX** (theoretical min): 48 / 23,015 (0.2%)

On NightHawk (rectangular grid, degree 4): even fewer dense blocks. Higher connectivity → fewer SWAPs → sparser blocks.

**Conclusion for standard benchmarks**: 3Q synthesis cannot help. Blocks are too small.

### Adversarial Circuits (8 circuits)

Circuits designed to maximize 3Q block density:

| Circuit | Type | 3Q Blocks | Median CX | Max CX | >14 CX | >20 CX |
|---------|------|:---------:|:---------:|:------:|:------:|:------:|
| **Multiplier_10** | **HRSCumulativeMultiplier (real)** | **2,267** | **9** | **33** | **273 (12%)** | **197 (8.7%)** |
| CDKM_Adder_40 | CDKMRippleCarryAdder (real) | 190 | 6 | 19 | 18 (9.5%) | 0 |
| DeepToffoliChain_60 | Overlapping CCX, 4 sweeps | 384 | 8 | 15 | 10 (2.6%) | 0 |
| VBE_Adder_20 | VBERippleCarryAdder (real) | 149 | 7 | 15 | 3 (2.0%) | 0 |
| MCX_Cascade_60 | Sliding MCX(4-ctrl) windows | 326 | 6 | 15 | 2 (0.6%) | 0 |
| RepeatedToffoli_60 | 15 CCX per group, same 3Q | 10 | 177 | 177 | 10 (100%) | 10 (100%) |
| ControlledRotation_60 | Repeated CRZ, fixed 3Q groups | 20 | 81 | 81 | 20 (100%) | 20 (100%) |

**The Multiplier is the critical finding**: a real arithmetic circuit producing 273 blocks (12%) above 14 CX, with max 33. CDKM Adder has 18 blocks (9.5%) at 15-19 CX.

RepeatedToffoli and ControlledRotation are synthetic stress tests — they confirm that heavy-hex topology does *not* inherently prevent dense blocks. The key factor is **circuit locality**, not topology degree.

### Separability: 5.4% CX Savings Without Resynthesis

Profiled all 12 standard benchmarks for bipartite separability (3Q block factors as 2Q ⊗ 1Q via SVD rank-1 check).

- **~2-6% of 3Q blocks are separable** (routing creates "bystander" qubits)
- **5.4% total CX savings** by splitting separable blocks and applying 2Q KAK to the sub-unitary
- Three circuit families see zero benefit: EfficientSU2, BV, GHZ (simple connectivity)
- **No resynthesis needed** — just factorization + existing 2Q pipeline

**Script**: `investigation/profile_3q_separability.py`

## End-to-End Experiment: 2Q-Only vs 3Q Block Synthesis

A/B comparison across 12 standard benchmarks. Both variants start from the same pre-optimized circuit; only block collection differs.

**Script**: `investigation/profile_2q_vs_3q_synthesis.py`

**Result**: 3Q synthesis regresses **every single circuit**.

- **Total**: 58,623 → 156,701 2Q gates (**+167%**)
- **Range**: +41.8% (Toffoli_90) to +697% (EfficientSU2_100)
- **Speed**: 18.7s → 128.1s (**5-8x slower**)
- **Root cause**: QSD produces ~20 CX per block for blocks that originally had 6-9 CX

Full per-circuit results in `investigation/profiling_output_2q_vs_3q.txt`.

## Which Circuits Would Benefit from 3Q Synthesis?

Based on all profiling, the circuits that produce dense 3Q blocks share a common trait: **deeply localized multi-qubit structure** — repeated operations on the same small set of qubits.

### Would benefit (blocks >14 CX found)

- **Integer multipliers** (HRS, controlled adder chains) — 8-12% of blocks >14 CX
- **Ripple-carry adders** (CDKM, VBE with deep carry chains) — up to 9.5% of blocks >14 CX
- **Modular arithmetic** (Shor's algorithm, modular exponentiation) — expected similar to multipliers
- **Circuits with repeated controlled operations on fixed qubit groups** — e.g., deep Toffoli sequences on same qubits

### Would NOT benefit (blocks too small)

- **QFT, QPE** — interactions spread across many qubit pairs
- **Quantum Volume, random circuits** — deliberately distributed connectivity
- **QAOA, Hamiltonian simulation** — interactions follow Hamiltonian terms, spread across lattice
- **Variational ansatze** (EfficientSU2) — shallow, sparse 2Q gates
- **Chain/GHZ circuits** — simple linear connectivity
- **Grover** — oracle interactions distributed across many qubits

The dividing line is **circuit locality**: does the algorithm repeatedly operate on the same 3-qubit neighborhood, or does it spread interactions across the lattice?

## Pytket's ThreeQubitSquash: A Working Reference

Pytket already has a production 3Q peephole optimizer. Understanding its design clarifies exactly what Qiskit is missing.

**Source**: `CQCL/tket` on GitHub — `ThreeQubitSquash.cpp`, `ThreeQubitConversion.cpp`, `CosSinDecomposition.cpp`

**Reference**: Mori et al., "Quantum circuit unoptimization," Phys. Rev. Research 7, 023139 (2025) — benchmarks Qiskit vs Pytket and notes ThreeQubitSquash "dramatically contributes to optimizing circuits."

### Algorithm: Cosine-Sine Decomposition (CSD)

Pytket uses CSD (not QSD) for 3Q synthesis. For an 8×8 unitary U:

1. **Separability check**: If any qubit is separable (tensor product), decompose as 1Q + 2Q KAK — skip full synthesis entirely
2. **CSD factorization**: U = [L0,0; 0,L1] · [C,-S; S,C] · [R0,0; 0,R1] via SVD + QR on block structure
3. **Right plex synthesis**: Synthesize [R0; R1] block-diagonal multiplexor. Tries **6 conjugation variants** (identity, SWAP, CX permutations) and picks the one with fewest CX
4. **Middle layer**: Cosine-sine rotation block → 3 CX + 1Q rotations
5. **Left plex synthesis**: Same as right, without diagonal extraction

**Max CX**: 20 for arbitrary 3Q unitary (8 + 3 + 9). Often fewer due to conjugation optimization.

### Gate Guard

Pytket only substitutes when synthesis produces **strictly fewer CX**:
```cpp
if (replacement.count_gates(target_2qb_gate_) < subc.count_gates(target_2qb_gate_))
```

This is exactly the guard that Qiskit's `ConsolidateBlocks` has for 2Q blocks but is **missing** for >2Q blocks.

### Pipeline Position

In `FullPeepholeOptimise`, ThreeQubitSquash runs after two rounds of 2Q optimization:
```
2Q_squash → 2Q_squash → ThreeQubitSquash → Clifford_simp → synthesis
```

### Comparison: Pytket vs Qiskit for 3Q

| Feature | Pytket ThreeQubitSquash | Qiskit >2Q Path |
|---------|------------------------|-----------------|
| Synthesis algorithm | CSD (SVD + QR) | QSD (Block ZXZ recursive) |
| Max CX (3Q) | 20 | ~20 |
| Separability check | Yes (before synthesis) | **No** |
| Conjugation optimization | 6 variants, pick best | **No** |
| Gate guard | Yes (strict improvement) | **No** (unconditional) |
| Unitary computation | C++ native | Rust→Python→NumPy roundtrip |
| Subcircuit finding | Greedy topological traversal | `CollectMultiQBlocks` (DSU) |

The max CX counts are similar (~20), but Pytket's practical CX count is often lower due to conjugation optimization + separability shortcut. And critically, the gate guard ensures it never regresses.

## Viable Optimization Directions

Informed by Pytket's design and our profiling. Ranked by expected impact:

### 1. Gate Guard for >2Q ConsolidateBlocks (Low Effort, Critical)

Add the missing gate guard to `consolidate_blocks.rs:347-403`. After computing the 3Q unitary, run QSD, count output CX, and only substitute if fewer than the original block. This is the single most important change — it prevents the +167% regression and is a prerequisite for all other 3Q work.

Pytket does exactly this. Without it, enabling 3Q collection is always a net loss.

**Effort**: Small — add CX count comparison in the >2Q branch. **Impact**: prevents regression, enables safe 3Q experimentation.

### 2. 3Q Separability Splitting (Low Effort, Broad Benefit)

Before full synthesis, check if the 8×8 unitary factors as 4×4 ⊗ 2×2 via SVD. If separable, split into 2Q + 1Q; existing KAK handles the rest. Pytket does this as a fast path before CSD. Our profiling shows **5.4% CX savings** across standard benchmarks from this alone.

**Effort**: Small — SVD rank-1 check (3 bipartitions). **Impact**: 5.4% CX reduction on all routed circuits.

### 3. Conjugation Optimization (Medium Effort, Improves Synthesis Quality)

Pytket tries 6 conjugation variants when synthesizing the multiplexor blocks and picks the one with fewest CX. This often reduces the practical CX count well below the 20 CX maximum. Could be added to Qiskit's QSD or implemented as a CSD-based alternative.

**Effort**: Medium — implement conjugation loop in `qsd.rs` or add CSD synthesis. **Impact**: reduces CX count on blocks where synthesis fires (arithmetic circuits).

### 4. CSD-Based 3Q Synthesis (Medium-High Effort, Parity with Pytket)

Replace or supplement QSD with a CSD-based 3Q synthesis (as Pytket uses). Combined with the gate guard and conjugation optimization, this would bring Qiskit's 3Q synthesis to feature parity with Pytket's ThreeQubitSquash.

**Effort**: Medium-high — implement CSD (SVD + QR + multiplexor synthesis) in Rust. **Impact**: comparable to Pytket's FullPeepholeOptimise for 3Q blocks.

### 5. Near-Optimal 3Q Synthesis (High Effort, Best Quality)

For maximum benefit on arithmetic circuits, a 14-15 CX synthesizer (AQC or SQUANDER-style variational) would capture blocks in the 15-20 CX range that even CSD misses. Our profiling shows 12% of Multiplier blocks and 9.5% of CDKM Adder blocks have >14 CX.

**Effort**: High — numerical optimization in Rust. **Impact**: targeted, arithmetic circuits only.

## Source Files

| File | Role |
|------|------|
| `crates/transpiler/src/passes/consolidate_blocks.rs` | Block consolidation: 2Q path (lines 404-452) vs >2Q path (lines 347-403) |
| `crates/transpiler/src/passes/unitary_synthesis/mod.rs` | Synthesis dispatch: 2Q→KAK (line 401), 3Q+→QSD (line 406) |
| `crates/synthesis/src/qsd.rs` | Quantum Shannon Decomposition |
| `crates/synthesis/src/two_qubit_decompose/weyl_decomposition.rs` | Weyl coordinates + `__num_basis_gates` (line 232) |
| `crates/synthesis/src/matrix/two_qubit.rs` | `blocks_to_matrix()` — Rust 2Q unitary computation (line 221) |
| `qiskit/transpiler/passes/optimization/collect_multiqubit_blocks.py` | N-qubit block collector (DSU algorithm) |
| `qiskit/synthesis/unitary/aqc/aqc.py` | Approximate Quantum Compiler (14 CX, not in default pipeline) |

## Research Papers

### 3Q Synthesis Bounds
- **Shende, Bullock, Markov** (quant-ph/0406176, 2004) — QSD. Lower bound: 14 CX for 3Q. Construction: ~20 CX.
- **Shende, Bullock, Markov** (quant-ph/0308033, 2004) — Proves 3 CX optimal for 2Q. KAK foundation.
- **Shende, Markov** (0803.2316, 2008) — 6 CX optimal for Toffoli.

### Near-Optimal 3Q Methods
- **Rakyta, Zimboras** (2109.06770, 2021) — 15 CX for arbitrary 3Q via variational optimization.
- **Krol, Al-Ars** (2403.13692, 2024) — 19 CX via Block ZXZ (exact, non-numerical).

### Compiler Benchmarking
- **Mori et al.** (PhysRevResearch.7.023139, 2025) — Quantum circuit unoptimization. Benchmarks Qiskit vs Pytket; notes Pytket's `ThreeQubitSquash` (CSD-based 3Q peephole) "dramatically contributes" to optimization. Shows generated circuits that are hard for both compilers.

### Block-Based Optimization
- **Weiden et al.** (2206.13645, 2022) — TopAS: numerical block synthesis, 30% CX reduction on 2D mesh.
- **Smith et al.** (2106.11246, 2021) — LEAP: 59x faster than QSearch for 4Q, up to 36x fewer CX.

## Next Steps

- [x] Profile standard benchmarks: 23K blocks, median 6-9 CX, <0.2% above 14 CX
- [x] Profile on NightHawk (rectangular grid): denser topology makes it worse
- [x] Research literature: 3Q synthesis algorithms and alternatives
- [x] Profile separability: 5.4% CX savings from splitting separable blocks
- [x] End-to-end experiment: +167% regression without gate guard
- [x] Document 2Q vs 3Q code path comparison
- [x] Profile adversarial circuits: Multiplier has 12% blocks >14 CX, max 33
- [x] Analyze Pytket's ThreeQubitSquash: CSD-based, gate guard, separability check, conjugation optimization
- [ ] Prototype gate guard for >2Q ConsolidateBlocks path
- [ ] Prototype 3Q separability splitting pass
- [ ] Run end-to-end 2Q vs 3Q on Multiplier with gate guard
- [ ] Evaluate CSD vs QSD synthesis quality on our benchmark blocks
