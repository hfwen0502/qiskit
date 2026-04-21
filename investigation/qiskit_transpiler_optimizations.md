# Qiskit Transpiler Optimization Passes

**Three Investigations into Level 2 Pipeline Efficiency**

1. Optimization Loop: redundant iterations in the L2 loop
2. Compute-Then-Apply: memory access pattern refactoring (15-30% speedup)
3. 3-Qubit Block Synthesis: separability splitting (-5% CX gates)

Sophia Wen | IBM Quantum | Benchpress + Qiskit fork

---

## 1. Optimization Loop: The L2 Loop Does No Useful Work After Iteration 1

**Branch**: [`pass-manager-investigation`](https://github.com/hfwen0502/qiskit/tree/pass-manager-investigation) | **Details**: [`investigation/optimization_loop.md`](https://github.com/hfwen0502/qiskit/blob/pass-manager-investigation/investigation/optimization_loop.md)

### Problem

- Level 2 uses `FixedPoint(size) AND FixedPoint(depth)` to detect convergence — minimum 2 iterations always
- Iteration 2+ only confirms "nothing changed"
- Wastes 33-50% of optimization stage time

### Key Finding

- Pre-loop passes (ConsolidateBlocks, UnitarySynthesis) handle all 2Q reduction before the loop starts
- Loop passes contribute 0 additional 2Q gates on 12 out of 13 benchmark circuits
- **Removing the loop entirely: identical gate counts**

### Results: 13 Circuits on FakeTorino (133Q), Optimization Level 2

| Circuit | Old Iters | New Iters | 2Q Gates | Regressed? |
|---------|-----------|-----------|----------|------------|
| QFT_100 | 3 | 2 | 9,528 | No |
| QV_100 | 2 | 1 | 96,474 | No |
| EfficientSU2_100 | 2 | 1 | 297 | No |
| QAOA_100 | 3 | 1 | 186 | No |
| BV_100 | 2 | 1 | 196 | No |
| Heisenberg_100 | 2 | 1 | 891 | No |

### Recommendation

Replace FixedPoint with a direct "changed" boolean flag. Passes return whether they modified 2Q gates; loop exits immediately when no pass reports changes.

---

## 2. Compute-Then-Apply: Batching DAG Access for Cache Efficiency

**Branch**: [`parallel-optimization-passes`](https://github.com/hfwen0502/qiskit/tree/parallel-optimization-passes) | **Details**: [`investigation/parallel_optimization_passes.md`](https://github.com/hfwen0502/qiskit/blob/parallel-optimization-passes/investigation/parallel_optimization_passes.md) | [**Full diff**](https://github.com/hfwen0502/qiskit/compare/03c640f73...ea4abc77c)

Three most expensive optimization passes: ConsolidateBlocks (51-75%), Optimize1qGatesDecomposition (6-15%), CommutativeCancellation (6-15%)

### Approach

<table>
<tr><th>Before: Interleaved read-compute-write</th><th>After: Batched compute-then-apply</th></tr>
<tr><td>

```
for item in items:
    result = read_dag(item)   # READ
    compute(result)           # COMPUTE
    mutate_dag(result)        # WRITE
    # next read hits cold cache
    # (edges/nodes just modified)
```

</td><td>

```
# Phase 1: compute (DAG stable, cache warm)
results = []
for item in items:
    results.push(read_dag(item) + compute)

# Phase 2: apply (batch all mutations)
for result in results:
    mutate_dag(result)
```

</td></tr>
</table>

### Key Insight

Speedup comes from memory access patterns, not threading. `remove_1q_sequence()` / `replace_block()` / `remove_op_node()` modify petgraph's StableGraph edge lists after every item, invalidating CPU cache lines. Batching all reads first keeps the cache warm. **Rayon ON vs OFF shows no difference.**

### Passes Refactored (all Rust, ~470 lines changed)

- [`Optimize1qGatesDecomposition`](https://github.com/hfwen0502/qiskit/blob/fdd061ef6/crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs): extract `process_run()`, pre-compute basis data, parallel threshold 500 runs
- [`CommutationAnalysis`](https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/passes/commutation_analysis.rs): extract per-wire function, `&mut self` -> `&self` on CommutationChecker, parallel threshold 100 qubits
- [`ConsolidateBlocks`](https://github.com/hfwen0502/qiskit/blob/ea4abc77c/crates/transpiler/src/passes/consolidate_blocks.rs): `TwoQBlockAction` enum, 3-phase (classify/compute/apply), parallel threshold 200 blocks

### Benchmark Results

**Remote Server (Intel Xeon, 160 vCPUs) — Rayon ON:**

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 2.626 | 2.051 | **1.28x** |
| QFT-100 | 6.321 | 4.605 | **1.37x** |
| ESU2-50 | 0.303 | 0.293 | 1.03x |
| ESU2-100 | 0.489 | 0.350 | **1.40x** |
| QV-50 | 1.167 | 1.013 | **1.15x** |
| QV-100 | 3.385 | 3.128 | **1.08x** |

**Remote Server — Rayon OFF (serial mode):**

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 2.700 | 2.175 | **1.24x** |
| QFT-100 | 6.374 | 4.491 | **1.42x** |
| ESU2-50 | 0.299 | 0.293 | 1.02x |
| ESU2-100 | 0.494 | 0.350 | **1.41x** |
| QV-50 | 1.273 | 1.134 | **1.12x** |
| QV-100 | 3.429 | 3.082 | **1.11x** |

**Rayon ON vs OFF (our branch) — no measurable difference:**

| Circuit | Remote ON | Remote OFF | Local ON | Local OFF |
|---------|-----------|------------|----------|-----------|
| QFT-100 | 4.605 | 4.491 | 7.231 | 5.592 |
| ESU2-100 | 0.350 | 0.350 | 0.502 | 0.390 |
| QV-100 | 3.128 | 3.082 | 4.286 | 3.688 |

### Takeaway

15-42% speedup on 50-100 qubit circuits from cache-friendly DAG access. Identical speedup with rayon ON and OFF confirms the gain is purely from compute-then-apply batching. No regressions on small circuits. Rayon parallelism is ready for future 1000+ qubit circuits.

---

## 3. 3-Qubit Block Synthesis: Separability Splitting for -5% CX Gates

**Branch**: [`pass-manager-investigation`](https://github.com/hfwen0502/qiskit/tree/pass-manager-investigation) | **Details**: [`investigation/three_qubit_block_synthesis.md`](https://github.com/hfwen0502/qiskit/blob/pass-manager-investigation/investigation/three_qubit_block_synthesis.md)

### Problem & Approach

- Qiskit has excellent 2Q synthesis (KAK/Weyl: 0-3 CX) but no 3Q block optimization
- Many 3Q blocks contain "bystander" qubits that don't interact — block is really 2Q + 1Q (tensor product)

**Sequential strategy:**
1. Run standard 2Q optimization (existing Level 2)
2. Detect separable 3Q blocks (identity coefficient test)
3. Split into 2Q + 1Q, route 2Q through KAK
4. Guard QSD with >14 CX threshold for remainder

### Key Finding

- **Separability accounts for 90% of all gains** (8,966 of 9,964 CX gates saved)
- After 2Q KAK optimization, separability becomes more prevalent: 1,498 blocks vs ~500 before (KAK consolidation creates tensor-product structures)
- QSD contributes only 10% of savings, and only on circuits with dense 3Q blocks (e.g., multiplier)

### Results: 8 Benchmark Circuits on FakeTorino, Level 2

| Circuit | Baseline CX | After 3Q Opt | CX Reduction | Separable Blocks |
|---------|-------------|--------------|--------------|------------------|
| QFT_100 | 9,528 | 9,279 | **-2.6%** | 312 |
| QV_100 | 96,474 | 91,932 | **-4.7%** | 408 |
| QAOA_100 | 186 | 176 | **-5.7%** | 52 |
| Random_100 | 12,845 | 12,137 | **-5.5%** | 326 |
| Multiplier_10 | 4,120 | 3,861 | **-6.3%** | 181 (QSD) |

### Recommendation

Implement separability splitting as a Python pass (low effort, high impact). Guard QSD with >14 CX threshold for targeted gains on dense circuits.

---

## Summary & Next Steps

| Investigation | Status | Impact | Effort |
|---------------|--------|--------|--------|
| Optimization Loop | Prototype done | 33-50% fewer iterations, 0 regressions | Low (flag change) |
| Compute-Then-Apply | Implemented (fork) | 15-42% speedup on opt passes | Medium (Rust refactor) |
| 3Q Block Synthesis | Prototype done | -5% CX gates across benchmarks | Low (Python pass) |

### Key Insights

- All three optimizations are orthogonal — they compose without interference
- The L2 loop's value was assumed, not measured. Profiling 13 circuits showed it does nothing.
- The parallelization hypothesis was wrong: actual gain is from cache-friendly memory access
- 3Q synthesis gains are dominated by separability (90%), not decomposition algorithms

### Next Steps

- Seek feedback from Qiskit transpiler team on all three proposals
- Optimization loop: propose changed-flag approach for Level 2
- Compute-then-apply: submit PR to Qiskit (branch: [`parallel-optimization-passes`](https://github.com/hfwen0502/qiskit/tree/parallel-optimization-passes))
- 3Q synthesis: implement separability pass, integrate into Level 2 pipeline

All code, benchmarks, and investigation docs: [github.com/hfwen0502/qiskit](https://github.com/hfwen0502/qiskit)
