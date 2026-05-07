# Investigation: 3-Qubit Block Collection and Synthesis

## Overview

The Qiskit team suggested exploring 3-qubit block synthesis as an optimization direction, inspired by Pytket's `ThreeQubitSquash`. Qiskit has state-of-the-art 2-qubit synthesis (KAK/Weyl, 0-3 CX, microseconds). The question: **can we extend this to 3-qubit blocks, and does it pay off?**

**Branch**: `pass-manager-investigation` (based on Qiskit main, commit `03c640f73`)

**Bottom line**: A sequential strategy — run 2Q KAK first, then 3Q optimization on the remaining blocks — yields a **-5.0% CX reduction** across 8 benchmarks with no regressions. The savings come primarily from **separability splitting** (90% of the benefit), not from full 3Q resynthesis. This can be implemented without modifying Qiskit's Rust core.

## Key Result: Sequential 2Q + 3Q Strategy

**Script and reference implementation**: [`profile_sequential_strategy.py`](investigation/profile_sequential_strategy.py) — contains the full working prototype including separability detection (ported from Pytket's `ThreeQubitConversion.cpp`), heuristic QSD guard, and end-to-end comparison. Runs on top of Qiskit without modifying any Qiskit source. Key functions: `check_separable()`, `run_3q_pass_simulated()`.

The optimal pipeline runs 2Q optimization first (existing Qiskit), then applies 3Q optimization on remaining blocks:

```
Step 1: Collect2qBlocks → ConsolidateBlocks → KAK synthesis     (existing Qiskit Level 2)
Step 2: CollectMultiQBlocks(3) → separability check → QSD+guard  (new, additive)
```

Step 2 uses two optimizations:
1. **Separability check** (cheap): test if 3Q block factors as 2Q ⊗ 1Q, then use KAK on the 2Q part
2. **Heuristic QSD guard**: only attempt QSD on blocks with >14 original CX (theoretical minimum)

| Circuit | After 2Q | After 2Q+3Q | Saved | % | Sep blocks | QSD replaced |
|---------|:--------:|:-----------:|:-----:|:-:|:----------:|:------------:|
| QFT_100 | 9,185 | 8,949 | 236 | -2.6% | 43 | 0 |
| QV_100 | 95,799 | 91,347 | 4,452 | -4.7% | 742 | 0 |
| QAOA_100 | 16,014 | 15,108 | 906 | -5.7% | 151 | 0 |
| Random_100 | 57,623 | 54,467 | 3,156 | -5.5% | 526 | 0 |
| Toffoli_90 | 511 | 511 | 0 | 0% | 0 | 0 |
| **Multiplier_10** | **18,831** | **17,641** | **1,190** | **-6.3%** | **32** | **181** |
| CDKM_Adder_40 | 1,369 | 1,345 | 24 | -1.8% | 4 | 0 |
| EfficientSU2_100 | 297 | 297 | 0 | 0% | 0 | 0 |
| **Total** | **199,629** | **189,665** | **9,964** | **-5.0%** | **1,498** | **181** |

**Savings breakdown**: separability = 8,966 CX (90%), QSD = 998 CX (10%), from only 213 QSD attempts.

### Why Sequential Order Matters

Running `CollectMultiQBlocks(3)` **instead of** `Collect2qBlocks` causes a +2.7% regression (profile_collector_comparison.py). The reason: the DSU-based multi-qubit collector greedily merges adjacent 2Q interactions (e.g., `CX(0,1)` + `CX(1,2)`) into 3Q blocks. These merged blocks typically have only 6-9 CX — too small for QSD to improve — but they can no longer be optimized as separate 2Q blocks by KAK. The merger destroys optimization opportunities.

The fix is Pytket's approach: run 2Q optimization **first** to exhaust all KAK opportunities, then run 3Q collection on the already-optimized circuit. This way, 3Q blocks only contain gates that KAK could not help.

### What Is Separability?

After routing on heavy-hex, SABRE inserts SWAP gates to move qubits along the topology. When `CollectMultiQBlocks` groups gates into 3Q blocks, some blocks contain a qubit that doesn't actually interact with the other two — a **"bystander" qubit** that was pulled in because it happens to be adjacent in the DAG.

Mathematically, a separable 3Q block has an 8×8 unitary that factors as a tensor product:

```
U_8×8 = V_4×4 ⊗ W_2×2    (2Q operation on qubits {a,b} + 1Q operation on qubit {c})
```

We detect this by checking all 3 bipartitions ({0}|{1,2}, {1}|{0,2}, {2}|{0,1}) using the `id_coeff` test from Pytket's `ThreeQubitConversion.cpp`: if `U_ij @ U_kl†` is a scalar multiple of identity for all 4×4 subblocks, the qubit is separable.

When a block is separable, we split it: the 2Q part goes through KAK (0-3 CX), the 1Q part is free. This replaces the original block (which had 6+ CX from routing overhead) with just 0-3 CX. The savings are large because routing creates many bystander situations.

**After 2Q optimization**, separability is even more prevalent (1,498 blocks found, up from ~500 on pre-optimized circuits), because KAK consolidation creates more tensor-product structures.

## Why Not Naively Enable 3Q Synthesis?

Qiskit already has all the pieces for 3Q: `CollectMultiQBlocks`, `ConsolidateBlocks` (>2Q path), and QSD in `UnitarySynthesis`. But enabling them naively regresses **every circuit** by +167% (profile_2q_vs_3q_synthesis.py).

**Root cause**: The >2Q path in `ConsolidateBlocks` has no gate guard — it unconditionally replaces blocks with `UnitaryGate`, and QSD produces ~20 CX regardless of input. For the typical 3Q block (median 6-9 CX), this triples the gate count.

## Code Paths: 2Q vs 3Q

### 2Q Call Chain (Current Pipeline)

```
Collect2qBlocks → dag.collect_2q_runs()              // Rust bicolor graph
ConsolidateBlocks (consolidate_blocks.rs:404-452)
  ① blocks_to_matrix()                                // Pure Rust: Matrix4
  ② num_basis_gates_inner(matrix)                      // Weyl → 0-3 CX, microseconds
  ③ GATE GUARD: if num_basis_gates < basis_count       // only replaces when cheaper
UnitarySynthesis → KAK/Weyl                            // PROVABLY OPTIMAL: 0-3 CX
```

### 3Q Call Chain (Existing but Broken)

```
CollectMultiQBlocks(max_block_size=3)                  // DSU-based grouping
ConsolidateBlocks (consolidate_blocks.rs:347-403)
  ① CircuitData::from_packed_operations()
  ② Python: Operator(circuit).data                     // Rust→Python→NumPy
  ③ NO GATE GUARD                                      // unconditional replacement
UnitarySynthesis → quantum_shannon_decomposition()     // QSD: ~20 CX ALWAYS
```

### What the 3Q Path is Missing

| Capability | 2Q Path | 3Q Path |
|---|---|---|
| Unitary computation | Pure Rust `Matrix4` | Rust→Python→NumPy roundtrip |
| Cost prediction | Weyl: exact CX in microseconds | None |
| Gate guard | `num_basis_gates < basis_count` | None — unconditional |
| Synthesis quality | KAK: optimal (0-3 CX) | QSD: ~20 CX (min possible: 14) |

## 3Q Synthesis Algorithms

| Algorithm | CX Gates (3Q) | Speed | Status |
|-----------|:-------------:|:-----:|--------|
| Theoretical lower bound | **14** | — | Proven (Shende et al., 2004) |
| SQUANDER (Rakyta & Zimboras) | **15** | ~100ms | C++ library |
| Block ZXZ (Krol & Al-Ars) | **19** | Fast | Paper (2024) |
| QSD in Qiskit | **~20** | ~1ms | Production |
| CSD in Pytket | **≤20** | ~1ms | Production |

For 2Q, KAK produces 0-3 CX — so even a 4-CX block benefits. For 3Q, best methods produce 14-20 CX — so only blocks with >14 CX benefit from resynthesis.

## CSD vs QSD: Is Pytket's Algorithm Better?

We ported Pytket's CSD algorithm to Python (`profile_csd_vs_qsd.py`) and compared against Qiskit's QSD on 1,325 blocks from 8 circuits.

**Result**: CSD's only advantage over QSD is separability detection. On non-separable blocks, QSD is equal or better.

| | CSD | QSD |
|---|---|---|
| Total CX savings (with gate guard) | 398 (4.2%) | 368 (3.8%) |
| Savings from separable blocks | ~390 | 0 |
| Max CX on random 3Q unitary | 20 (Pytket) / 23 (our port) | ~19 |

**Conclusion**: Implementing full CSD in Qiskit is not worth the effort. The optimal path is: **separability check (from CSD) + existing QSD (from Qiskit)**.

## Pytket's ThreeQubitSquash

**Source**: `CQCL/tket` — `ThreeQubitSquash.cpp`, `ThreeQubitConversion.cpp`, `CosSinDecomposition.cpp`

Key design elements we adopt:
- **Pipeline position**: 3Q runs after 2Q squash (we adopt this)
- **Gate guard**: only substitute if fewer CX (we adopt this)
- **Separability check**: tensor product detection before synthesis (we adopt this)

Elements we don't need:
- **CSD synthesis**: QSD is equal or better on non-separable blocks
- **Conjugation optimization**: absorbed into CSD, not applicable to QSD
- **Second 2Q pass**: our testing shows it yields zero improvement after KAK

## 3Q Block Profiling

All on FakeTorino (133Q, heavy-hex), Level 2 pre-optimization.

### Standard Benchmarks (12 circuits)

23,015 blocks across QFT, QV, QAOA, Grover, Heisenberg, Adder, Random, GHZ, QPE, BV, EfficientSU2, Toffoli. Median CX per block: 6-9. Blocks >14 CX: 48/23,015 (0.2%). **Script**: `profile_3q_blocks.py`

### Adversarial Circuits (8 circuits)

| Circuit | 3Q Blocks | >14 CX | Max CX |
|---------|:---------:|:------:|:------:|
| **Multiplier_10** | **2,267** | **273 (12%)** | **33** |
| CDKM_Adder_40 | 190 | 18 (9.5%) | 19 |
| DeepToffoliChain_60 | 384 | 10 (2.6%) | 15 |
| Others | 10-326 | 0-3 | 15-177 |

**Script**: `profile_3q_adversarial.py`

The Multiplier is the only real circuit with significant blocks >14 CX. This is where QSD resynthesis (with gate guard) provides the additional 998 CX savings beyond separability.

## Viable Optimization Directions

### 1. Separability Splitting (Low Effort, Highest Impact)

Check if 3Q unitary factors as 2Q ⊗ 1Q. If so, split and use existing KAK. This provides **90% of the total 3Q optimization benefit** (8,966 of 9,964 CX saved).

**Effort**: Small — 3 bipartition checks per block. **Impact**: -5% CX across all circuit types.

### 2. Heuristic QSD Guard (Low Effort, Targeted Impact)

Only attempt QSD on blocks with >14 original CX. This avoids expensive synthesis on the vast majority of blocks while capturing dense arithmetic blocks.

**Effort**: Small — CX count threshold check. **Impact**: additional -1% on arithmetic circuits (Multiplier: 181 blocks improved, 998 CX saved).

### 3. ~~Full CSD Implementation~~ (Not Recommended)

CSD provides no advantage over QSD on non-separable blocks. Separability detection (Direction #1) captures CSD's only win.

### 4. Near-Optimal 3Q Synthesis (High Effort, Future)

A 14-15 CX synthesizer would capture blocks in the 15-20 CX range. Only relevant for arithmetic circuits.

## Source Files

| File | Role |
|------|------|
| `consolidate_blocks.rs:404-452` | 2Q path (gate guard) |
| `consolidate_blocks.rs:347-403` | >2Q path (no gate guard) |
| `unitary_synthesis/mod.rs:401-404` | 2Q → KAK |
| `unitary_synthesis/mod.rs:406-422` | 3Q+ → QSD |
| `collect_multiqubit_blocks.py` | DSU-based N-qubit block collector |

## Investigation Scripts

| Script | What it does |
|--------|-------------|
| `profile_3q_blocks.py` | Block statistics on 12 standard circuits |
| `profile_3q_adversarial.py` | Block statistics on 8 adversarial circuits |
| `profile_3q_separability.py` | Separability analysis on pre-optimized circuits |
| `profile_2q_vs_3q_synthesis.py` | End-to-end 2Q vs 3Q (+167% regression) |
| `profile_csd_vs_qsd.py` | CSD vs QSD synthesis quality (CSD port from Pytket) |
| `profile_collector_comparison.py` | Collect2qBlocks vs CollectMultiQBlocks (+2.7% regression) |
| `profile_sequential_strategy.py` | **Final strategy**: 2Q then 3Q (-5.0% improvement) |

## Research Papers

- **Shende, Bullock, Markov** (quant-ph/0406176, 2004) — QSD. Lower bound: 14 CX for 3Q.
- **Shende, Bullock, Markov** (quant-ph/0308033, 2004) — 3 CX optimal for 2Q (KAK).
- **Rakyta, Zimboras** (2109.06770, 2021) — 15 CX for arbitrary 3Q via variational optimization.
- **Krol, Al-Ars** (2403.13692, 2024) — 19 CX via Block ZXZ.
- **Mori et al.** (PhysRevResearch.7.023139, 2025) — Pytket's `ThreeQubitSquash` "dramatically contributes" to optimization.

## Completed

- [x] Profile standard benchmarks: 23K blocks, median 6-9 CX, <0.2% above 14 CX
- [x] Profile adversarial circuits: Multiplier has 12% blocks >14 CX, max 33
- [x] Profile separability: 2-6% of blocks separable after routing
- [x] End-to-end 2Q vs 3Q: +167% regression without gate guard
- [x] Analyze Pytket's ThreeQubitSquash: CSD + gate guard + separability
- [x] Port CSD to Python, compare vs QSD: CSD's only win is separability
- [x] Test replacing Collect2qBlocks with CollectMultiQBlocks: +2.7% regression
- [x] Test sequential strategy (2Q then 3Q): **-5.0% improvement, no regressions**
- [x] Identify optimal implementation: separability check + heuristic QSD guard
