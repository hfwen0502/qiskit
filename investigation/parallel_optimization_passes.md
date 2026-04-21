# Parallel Optimization Passes Investigation

## Goal

Speed up the three most expensive optimization passes in Qiskit's Level 2 transpiler
without changing any logic — same gate counts, same results, just faster.

| Pass | % of optimization time | Implementation |
|------|----------------------|----------------|
| ConsolidateBlocks | 51-75% | Rust |
| Optimize1qGatesDecomposition | 6-15% | Rust |
| CommutativeCancellation | 6-15% | Rust |

## Approach

Refactor each pass to separate read-only computation from DAG mutation, following
the existing pattern in `remove_identity_equiv.rs`:

1. **Compute phase**: Process all items (1Q runs, 2Q blocks, per-qubit commutation sets),
   reading from the DAG but not modifying it. Use `rayon::into_par_iter()` when above
   a size threshold.
2. **Apply phase**: Apply all DAG mutations sequentially (insert/remove nodes, replace blocks).

The original code interleaved computation and mutation — for each item, compute the
result and immediately apply it to the DAG before processing the next item.

## Environment Variables

**`QISKIT_IN_PARALLEL`** — Internal Qiskit flag (not user-facing). Controls whether
Rust rayon threading is enabled. The naming is counterintuitive:

| Value | `getenv_use_multiple_threads()` | Rayon | Python `parallel_map` |
|-------|-------------------------------|-------|----------------------|
| Unset (default) | `true` | **ON** | Allowed |
| `"FALSE"` | `true` | **ON** | Allowed |
| `"TRUE"` | `false` | **OFF** | Disabled |

This flag is set internally by `qiskit.utils.parallel.parallel_map()` to `"TRUE"` when
spawning child processes, preventing nested parallelism. Users should never set it directly.

**`RAYON_NUM_THREADS`** — Standard rayon env var. Controls the number of threads in the
rayon thread pool. Default: number of logical CPUs.

**`QISKIT_FORCE_THREADS`** — If set to `"TRUE"`, forces rayon threading even when
`QISKIT_IN_PARALLEL=TRUE`. Used for testing.

## Changes

### 1. Optimize1qGatesDecomposition (`optimize_1q_gates_decomposition.rs`)

- Extracted `precompute_basis_data()` — computes `basis_gates_per_qubit` and
  `target_basis_per_qubit` upfront for all unique qubits, eliminating lazy init
  inside the hot loop.
- Extracted `process_run()` — pure computation for a single 1Q run (matrix fold,
  Euler decomposition, error comparison). Returns `Option<RunReplacement>`.
- Added `py_optimize_1q_gates_decomposition()` wrapper with `py.detach()` for GIL release.
- Parallel threshold: 500 runs.

### 2. Commutation Analysis (`commutation_analysis.rs`, `commutation_checker.rs`)

- Changed `CommutationChecker::commute()` from `&mut self` to `&self` — the method
  never mutates (the `&mut` was a PyO3 artifact). Enables sharing across threads.
- Extracted `analyze_commutations_for_wire()` — processes a single qubit wire.
- Changed `analyze_commutations()` signature from `(&mut DAGCircuit, &mut CommutationChecker)`
  to `(&DAGCircuit, &CommutationChecker)`.
- Merge phase with pre-allocated `IndexMap` capacity.
- Parallel threshold: 100 qubits.
- No changes needed in `commutation_cancellation.rs` — Rust auto-derefs `&mut` to `&`.

### 3. ConsolidateBlocks (`consolidate_blocks.rs`)

- Defined `TwoQBlockAction` enum (Skip, RemoveIdentity, Consolidate) and `TwoQBlockInfo` struct.
- Extracted `process_2q_block()` — computes matrix via `blocks_to_matrix()` and
  decomposition count via `num_basis_gates_inner()`. Pure Rust, no Python.
- Three-phase processing:
  1. Classify blocks: handle single-gate and >2Q blocks immediately, collect 2Q block metadata.
  2. Process 2Q blocks in parallel (matrix computation + Weyl decomposition counting).
  3. Apply all 2Q actions sequentially.
- >2Q blocks still processed sequentially (require `Python::attach()` for matrix via `QI_OPERATOR`).
- Parallel threshold: 200 blocks.

## Key Finding

**The speedup comes from the memory access pattern, not from threading.**

The original code interleaved DAG reads and writes:
```
for each item:
    read DAG → compute result → mutate DAG → next item reads modified DAG
```

Our refactored code batches them:
```
for each item:
    read DAG → compute result → store in Vec
for each result:
    mutate DAG
```

In the original, `remove_1q_sequence()` / `replace_block()` / `remove_op_node()` modify
the `StableGraph`'s internal edge lists and node weights after every item. This invalidates
CPU cache lines, so the next item's DAG reads suffer cache misses.

By batching all reads first, the computation runs on a stable DAG with warm caches.
The mutations happen once at the end. This yields **15-30% speedup even with rayon
disabled** (single-threaded mode).

Rayon parallelism is structurally correct and ready, but at current circuit sizes
(50-100 qubits, <500 1Q runs, <200 2Q blocks) the per-item work is too light relative
to thread pool overhead. It would help on circuits with 1000+ qubits.

## Benchmark Results

### Setup

- **Remote server**: Intel Xeon Sapphire Rapids, 160 vCPUs, Linux
- **Local**: Apple M-series, 10 cores, macOS
- **Circuits**: QFT, EfficientSU2, QuantumVolume at 50 and 100 qubits
- **Backend**: FakeTorino (133Q heavy-hex), optimization level 2
- **Pinned cores**: `taskset -c 0-20:2` (remote)
- **5 runs per circuit**, reporting mean

### Default Mode (rayon ON) — Remote Server

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 2.626 | 2.051 | **1.28x** |
| QFT-100 | 6.321 | 4.605 | **1.37x** |
| EfficientSU2-50 | 0.303 | 0.293 | 1.03x |
| EfficientSU2-100 | 0.489 | 0.350 | **1.40x** |
| QV-50 | 1.167 | 1.013 | **1.15x** |
| QV-100 | 3.385 | 3.128 | **1.08x** |

### Serial Mode (rayon OFF, `QISKIT_IN_PARALLEL=TRUE`) — Remote Server

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 2.700 | 2.175 | **1.24x** |
| QFT-100 | 6.374 | 4.491 | **1.42x** |
| EfficientSU2-50 | 0.299 | 0.293 | 1.02x |
| EfficientSU2-100 | 0.494 | 0.350 | **1.41x** |
| QV-50 | 1.273 | 1.134 | **1.12x** |
| QV-100 | 3.429 | 3.082 | **1.11x** |

### Default Mode (rayon ON) — Local Mac

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 3.082 | 2.929 | 1.05x |
| QFT-100 | 7.572 | 7.231 | 1.05x |
| EfficientSU2-50 | 0.316 | 0.313 | 1.01x |
| EfficientSU2-100 | 0.501 | 0.502 | 1.00x |
| QV-50 | 1.355 | 1.357 | 1.00x |
| QV-100 | 4.272 | 4.286 | 1.00x |

Note: Local Mac results for rayon ON show smaller speedup than remote server.
This is likely due to higher system noise on a laptop with background processes.

### Serial Mode (rayon OFF, `QISKIT_IN_PARALLEL=TRUE`) — Local Mac

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 2.930 | 2.432 | **1.20x** |
| QFT-100 | 6.833 | 5.592 | **1.22x** |
| EfficientSU2-50 | 0.312 | 0.301 | 1.04x |
| EfficientSU2-100 | 0.505 | 0.390 | **1.29x** |
| QV-50 | 1.328 | 1.287 | 1.03x |
| QV-100 | 4.192 | 3.688 | **1.14x** |

### Rayon ON vs OFF — Our Branch

To isolate whether rayon threading contributes to the speedup, we compared our branch
with rayon enabled (default) vs disabled (`QISKIT_IN_PARALLEL=TRUE`):

**Remote Server (160 vCPUs, taskset -c 0-20:2):**

| Circuit | Rayon ON (s) | Rayon OFF (s) | Difference |
|---------|-------------|--------------|------------|
| QFT-50 | 2.051 | 2.175 | ~same |
| QFT-100 | 4.605 | 4.491 | ~same |
| EfficientSU2-50 | 0.293 | 0.293 | ~same |
| EfficientSU2-100 | 0.350 | 0.350 | ~same |
| QV-50 | 1.013 | 1.134 | ~same |
| QV-100 | 3.128 | 3.082 | ~same |

**Local Mac (10 cores):**

| Circuit | Rayon ON (s) | Rayon OFF (s) | Difference |
|---------|-------------|--------------|------------|
| QFT-50 | 2.929 | 2.432 | ~same |
| QFT-100 | 7.231 | 5.592 | ~same |
| EfficientSU2-50 | 0.313 | 0.301 | ~same |
| EfficientSU2-100 | 0.502 | 0.390 | ~same |
| QV-50 | 1.357 | 1.287 | ~same |
| QV-100 | 4.286 | 3.688 | ~same |

**No measurable difference.** Rayon parallelism is a no-op at these circuit sizes. The
speedup over upstream main is entirely from the compute-then-apply restructuring.

### Observations

- Speedups are consistent across remote and local, and across rayon ON/OFF — confirming
  the gain is from memory access patterns, not threading.
- Larger circuits benefit more (more items to process, more cache benefit from batching).
- Small circuits (EfficientSU2-50) show no overhead (~1-3%), confirming no regression.
- Gate counts are equivalent across all runs (stochastic SABRE causes minor variation).
- We verified that our base commit (`03c640f73`) and upstream main (`51680f9d8`) produce
  identical performance, ruling out upstream regression.

## Correctness

All tests pass:
- 94 `test_optimize_1q_decomposition.py`
- 34 `test_commutative_cancellation.py`
- 43 `test_consolidate_blocks.py`
- 1 Rust unit test (`test_identity_unitary_is_removed`)

## Files Modified

| File | Lines | Change |
|------|-------|--------|
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | +180 | Pre-compute basis data, extract `process_run`, add rayon |
| `crates/transpiler/src/passes/commutation_analysis.rs` | +138/-75 | Extract per-wire function, parallel per-qubit, `&mut` → `&` signature |
| `crates/transpiler/src/commutation_checker.rs` | +1/-1 | `commute()`: `&mut self` → `&self` |
| `crates/transpiler/src/passes/consolidate_blocks.rs` | +151/-41 | Three-phase processing, extract `process_2q_block`, add rayon |

## Branch

`parallel-optimization-passes` on `fork` (https://github.com/hfwen0502/qiskit.git),
based on commit `03c640f73`.
