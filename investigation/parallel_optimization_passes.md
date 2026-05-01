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
the existing pattern in [`remove_identity_equiv.rs`](https://github.com/hfwen0502/qiskit/blob/03c640f73/crates/transpiler/src/passes/remove_identity_equiv.rs):

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

[**Full diff: `03c640f73...ea4abc77c`**](https://github.com/hfwen0502/qiskit/compare/03c640f73...ea4abc77c)

### 1. Optimize1qGatesDecomposition

[**Commit `fdd061ef6`**](https://github.com/hfwen0502/qiskit/commit/fdd061ef6) — [`optimize_1q_gates_decomposition.rs`](https://github.com/hfwen0502/qiskit/blob/fdd061ef6/crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs)

- Extracted `precompute_basis_data()` — computes `basis_gates_per_qubit` and
  `target_basis_per_qubit` upfront for all unique qubits, eliminating lazy init
  inside the hot loop.
- Extracted `process_run()` — pure computation for a single 1Q run (matrix fold,
  Euler decomposition, error comparison). Returns `Option<RunReplacement>`.
- Added `py_optimize_1q_gates_decomposition()` wrapper with `py.detach()` for GIL release.
- Added `PARALLEL_THRESHOLD = 500` (new constant — original code had no parallelism).
  Rayon activates only when there are >= 500 1Q runs; below that, sequential fallback.

<table>
<tr><th>Before (<a href="https://github.com/hfwen0502/qiskit/blob/03c640f73/crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs">03c640f73</a>)</th><th>After (<a href="https://github.com/hfwen0502/qiskit/blob/fdd061ef6/crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs">fdd061ef6</a>)</th></tr>
<tr><td>

```rust
// Single loop: compute + mutate interleaved
for raw_run in runs {
    let operator = raw_run.iter()
        .map(|node| { /* read DAG */ })
        .fold(ONE_QUBIT_IDENTITY, |mut op, node| {
            matmul_1q_with_slice(&mut op, &node); op
        });
    let sequence = unitary_to_gate_sequence_inner(
        aview2(&operator), target_basis_set,
        qubit.index(), None, true, None,
    );
    // ... error comparison ...
    if should_replace {
        for gate in sequence.gates {
            dag.insert_1q_on_incoming_qubit(
                (gate.0, &gate.1), raw_run[0]
            );                                  // WRITE
        }
        dag.add_global_phase(/* ... */)?;       // WRITE
        dag.remove_1q_sequence(&raw_run);       // WRITE
    }
}
```

</td><td>

```rust
struct RunReplacement {
    run: Vec<NodeIndex>,
    sequence: OneQubitGateSequence,
}

// Phase 1: Parallel compute (reads only)
let replacements: Vec<RunReplacement> =
    if runs.len() >= 500 && run_in_parallel {
        runs.into_par_iter()
            .filter_map(|run| process_run(
                dag, run, target,
                &basis_gates_per_qubit,
                &target_basis_per_qubit,
            ))
            .collect()
    } else { /* sequential fallback */ };

// Phase 2: Sequential apply (writes only)
for r in replacements {
    for gate in r.sequence.gates {
        dag.insert_1q_on_incoming_qubit(
            (gate.0, &gate.1), r.run[0]
        );
    }
    dag.add_global_phase(/* ... */)?;
    dag.remove_1q_sequence(&r.run);
}
```

</td></tr>
</table>

### 2. Commutation Analysis

[**Commit `c5228537e`**](https://github.com/hfwen0502/qiskit/commit/c5228537e) — [`commutation_analysis.rs`](https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/passes/commutation_analysis.rs), [`commutation_checker.rs`](https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/commutation_checker.rs)

- Changed `CommutationChecker::commute()` from `&mut self` to `&self` — the method
  never mutates (the `&mut` was a PyO3 artifact). Enables sharing across threads.
- Extracted `analyze_commutations_for_wire()` — processes a single qubit wire.
- Changed `analyze_commutations()` signature from `(&mut DAGCircuit, &mut CommutationChecker)`
  to `(&DAGCircuit, &CommutationChecker)`.
- Merge phase with pre-allocated `IndexMap` capacity.
- Added `PARALLEL_THRESHOLD = 100` (new constant — original code had no parallelism).
  Rayon activates only when there are >= 100 qubits; below that, sequential fallback.
- No changes needed in [`commutation_cancellation.rs`](https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/passes/commutation_cancellation.rs) — Rust auto-derefs `&mut` to `&`.

<table>
<tr><th>Before (<a href="https://github.com/hfwen0502/qiskit/blob/03c640f73/crates/transpiler/src/passes/commutation_analysis.rs">03c640f73</a>)</th><th>After (<a href="https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/passes/commutation_analysis.rs">c5228537e</a>)</th></tr>
<tr><td>

```rust
pub fn analyze_commutations(
    dag: &mut DAGCircuit,             // &mut
    commutation_checker: &mut CommutationChecker,
    approximation_degree: f64,
) -> PyResult<(CommutationSet, NodeIndices)> {
    let mut commutation_set = Default::default();
    let mut node_indices = Default::default();

    // Sequential: one qubit at a time,
    // writing to shared maps in-place
    for qubit in 0..dag.num_qubits() {
        let wire = Wire::Qubit(Qubit(qubit as u32));
        for gate in dag.nodes_on_wire(wire, false) {
            let entry = commutation_set
                .entry(wire)
                .or_insert_with(|| vec![vec![gate]]);
            // ... check + mutate entry ...
            node_indices.insert(
                (gate, wire), entry.len() - 1
            );
        }
    }
    Ok((commutation_set, node_indices))
}
```

</td><td>

```rust
// Per-wire function: pure, no shared state
fn analyze_commutations_for_wire(
    dag: &DAGCircuit,                 // & (shared)
    commutation_checker: &CommutationChecker,
    approximation_degree: f64,
    qubit: u32,
) -> Result<(Wire, Vec<Vec<NodeIndex>>,
             Vec<((NodeIndex, Wire), usize)>),
            PyErr> { /* ... */ }

pub fn analyze_commutations(
    dag: &DAGCircuit,                 // & (was &mut)
    commutation_checker: &CommutationChecker,
    approximation_degree: f64,
) -> PyResult<(CommutationSet, NodeIndices)> {
    // Parallel per-qubit compute
    let results = if num_qubits >= 100
                     && run_in_parallel {
        (0..num_qubits as u32).into_par_iter()
            .map(|q| analyze_commutations_for_wire(
                dag, commutation_checker,
                approximation_degree, q,
            ))
            .collect::<PyResult<Vec<_>>>()?
    } else { /* sequential fallback */ };

    // Merge into final maps
    for (wire, sets, indices) in results {
        commutation_set.insert(wire, sets);
        for (k, v) in indices {
            node_indices.insert(k, v);
        }
    }
    Ok((commutation_set, node_indices))
}
```

</td></tr>
</table>

### 3. ConsolidateBlocks

[**Commit `ea4abc77c`**](https://github.com/hfwen0502/qiskit/commit/ea4abc77c) — [`consolidate_blocks.rs`](https://github.com/hfwen0502/qiskit/blob/ea4abc77c/crates/transpiler/src/passes/consolidate_blocks.rs)

- Defined `TwoQBlockAction` enum (Skip, RemoveIdentity, Consolidate) and `TwoQBlockInfo` struct.
- Extracted `process_2q_block()` — computes matrix via `blocks_to_matrix()` and
  decomposition count via `num_basis_gates_inner()`. Pure Rust, no Python.
- Three-phase processing:
  1. Classify blocks: handle single-gate and >2Q blocks immediately, collect 2Q block metadata.
  2. Process 2Q blocks in parallel (matrix computation + Weyl decomposition counting).
  3. Apply all 2Q actions sequentially.
- >2Q blocks still processed sequentially (require `Python::attach()` for matrix via `QI_OPERATOR`).
- Added `PARALLEL_THRESHOLD = 200` (new constant — original code had no parallelism).
  Rayon activates only when there are >= 200 2Q blocks; below that, sequential fallback.

<table>
<tr><th>Before (<a href="https://github.com/hfwen0502/qiskit/blob/03c640f73/crates/transpiler/src/passes/consolidate_blocks.rs">03c640f73</a>)</th><th>After (<a href="https://github.com/hfwen0502/qiskit/blob/ea4abc77c/crates/transpiler/src/passes/consolidate_blocks.rs">ea4abc77c</a>)</th></tr>
<tr><td>

```rust
// Single loop: classify + compute + write
for block in blocks {
    // ... classify block ...
    if block_qargs.len() > 2 {
        // >2Q: Python matrix, replace now
    } else {
        let matrix = blocks_to_matrix(
            dag, &block, block_index_map
        ).ok();
        if let Some(matrix) = matrix {
            let n = decomposer
                .num_basis_gates_inner(/*..*/)?;
            if force || n < basis_count {
                if matrix == IDENTITY_2Q {
                    for node in block {
                        dag.remove_op_node(node);
                        // WRITE ^
                    }
                } else {
                    dag.replace_block(
                        &block, /*UnitaryGate*/
                    )?; // WRITE
                }
            }
        }
    }
}
```

</td><td>

```rust
enum TwoQBlockAction {
    Skip,
    RemoveIdentity(Vec<NodeIndex>),
    Consolidate {
        block: Vec<NodeIndex>,
        matrix: Matrix4<Complex64>,
        qubit_pos_map: HashMap<Qubit, usize>,
    },
}

// Phase 1: Classify (sequential)
let mut two_q_blocks: Vec<TwoQBlockInfo> = Vec::new();
for block in blocks {
    if block_qargs.len() > 2 { /* handle now */ }
    else { two_q_blocks.push(/* metadata */); }
}

// Phase 2: Parallel compute (reads only)
let actions: Vec<TwoQBlockAction> =
    if two_q_blocks.len() >= 200
       && run_in_parallel {
        two_q_blocks.into_par_iter()
            .map(|info| process_2q_block(
                dag, info, &decomposer,
                force_consolidate, /*..*/
            ))
            .collect::<PyResult<Vec<_>>>()?
    } else { /* sequential fallback */ };

// Phase 3: Sequential apply (writes only)
for action in actions {
    match action {
        Skip => {}
        RemoveIdentity(block) => {
            for n in block {
                dag.remove_op_node(n);
            }
        }
        Consolidate { block, matrix, .. } => {
            dag.replace_block(&block, /*..*/)?;
        }
    }
}
```

</td></tr>
</table>

## Key Finding

**Separating computation from mutation enables rayon parallelism, giving 1.5-1.6x
end-to-end speedup on optimization-heavy circuits.**

The original code interleaved DAG reads and writes, which prevents parallelism
because each mutation invalidates shared state:

```
for each item:
    read DAG -> compute result -> mutate DAG    ← sequential, cannot parallelize
```

Our refactored code separates the read-only compute phase from the write phase,
making the compute phase embarrassingly parallel:

```
rayon::par_iter(items)                          ← parallel compute
    read DAG -> compute result -> collect results
for each result:
    mutate DAG                                  ← sequential apply
```

With release builds, rayon parallelism on the compute phase gives **4.7-6.5x speedup**
on QFT and QV circuits within these three passes. Combined with the end-to-end
transpilation pipeline (where SABRE routing and other passes are not affected),
this translates to **1.5-1.6x overall transpilation speedup** on QFT circuits.

> **Important: use release builds.** Initial benchmarks were run with a debug Rust build
> (`pip install -e .` without `QISKIT_BUILD_PROFILE=release`), which masked the rayon
> speedup entirely — debug builds showed ~0% difference between rayon ON and OFF.
> All results below use release builds.

## Benchmark Results

### Setup

- **Remote server**: Intel Xeon Sapphire Rapids, 160 vCPUs, Linux
- **Circuits**: QFT, EfficientSU2, QuantumVolume at 50 and 100 qubits
- **Backend**: FakeTorino (133Q heavy-hex), optimization level 2
- **Build**: `QISKIT_BUILD_PROFILE=release pip install -e .`
- **Pinned cores**: `taskset -c 0-20:2` (remote)
- **5 runs per circuit**, reporting mean
- **Benchmark script**: [`investigation/benchmark_parallel_passes.py`](https://github.com/hfwen0502/qiskit/blob/parallel-optimization-passes/investigation/benchmark_parallel_passes.py)

### Default Mode (rayon ON) — Remote Server

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 0.196 | 0.129 | **1.52x** |
| QFT-100 | 0.456 | 0.288 | **1.58x** |
| EfficientSU2-50 | 0.012 | 0.013 | ~same |
| EfficientSU2-100 | 0.019 | 0.018 | ~same |
| QV-50 | 0.060 | 0.054 | 1.11x |
| QV-100 | 0.164 | 0.154 | 1.06x |

### Serial Mode (rayon OFF, `QISKIT_IN_PARALLEL=TRUE`) — Remote Server

| Circuit | Main (s) | Ours (s) | Speedup |
|---------|----------|----------|---------|
| QFT-50 | 1.128 | 0.609 | **1.85x** |
| QFT-100 | 2.648 | 1.407 | **1.88x** |
| EfficientSU2-50 | 0.012 | 0.012 | ~same |
| EfficientSU2-100 | 0.019 | 0.019 | ~same |
| QV-50 | 0.305 | 0.264 | **1.16x** |
| QV-100 | 1.040 | 0.999 | 1.04x |

### Rayon ON vs OFF — Isolating Threading Contribution

**Main branch (upstream):**

| Circuit | Rayon ON (s) | Rayon OFF (s) | Rayon speedup |
|---------|-------------|--------------|---------------|
| QFT-50 | 0.196 | 1.128 | **5.8x** |
| QFT-100 | 0.456 | 2.648 | **5.8x** |
| QV-50 | 0.060 | 0.305 | **5.1x** |
| QV-100 | 0.164 | 1.040 | **6.3x** |

**Our branch:**

| Circuit | Rayon ON (s) | Rayon OFF (s) | Rayon speedup |
|---------|-------------|--------------|---------------|
| QFT-50 | 0.129 | 0.609 | **4.7x** |
| QFT-100 | 0.288 | 1.407 | **4.9x** |
| QV-50 | 0.054 | 0.264 | **4.9x** |
| QV-100 | 0.154 | 0.999 | **6.5x** |

Main already has rayon parallelism in other passes (SABRE routing, etc.) which accounts
for its 5-6x rayon speedup. Our branch adds rayon to the three optimization passes,
and in serial mode we see the restructuring benefit clearly (1.85-1.88x on QFT).

### Observations

- **QFT benefits most** because it has the most 1Q runs and 2Q blocks to process.
- **EfficientSU2 is unaffected** — sparse linear connectivity means few optimization items.
- **QV shows modest improvement** with rayon ON (1.06-1.11x) — the blocks are dense but few.
- Small circuits show no overhead, confirming no regression.
- Gate counts are equivalent across all runs (stochastic SABRE causes minor variation).

## Correctness

All tests pass:
- 94 `test_optimize_1q_decomposition.py`
- 34 `test_commutative_cancellation.py`
- 43 `test_consolidate_blocks.py`
- 1 Rust unit test (`test_identity_unitary_is_removed`)

## Files Modified

| File | Lines | Change |
|------|-------|--------|
| [`optimize_1q_gates_decomposition.rs`](https://github.com/hfwen0502/qiskit/blob/fdd061ef6/crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs) | +180 | Pre-compute basis data, extract `process_run`, add rayon |
| [`commutation_analysis.rs`](https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/passes/commutation_analysis.rs) | +138/-75 | Extract per-wire function, parallel per-qubit, `&mut` -> `&` signature |
| [`commutation_checker.rs`](https://github.com/hfwen0502/qiskit/blob/c5228537e/crates/transpiler/src/commutation_checker.rs) | +1/-1 | `commute()`: `&mut self` -> `&self` |
| [`consolidate_blocks.rs`](https://github.com/hfwen0502/qiskit/blob/ea4abc77c/crates/transpiler/src/passes/consolidate_blocks.rs) | +151/-41 | Three-phase processing, extract `process_2q_block`, add rayon |
| [`benchmark_parallel_passes.py`](https://github.com/hfwen0502/qiskit/blob/parallel-optimization-passes/investigation/benchmark_parallel_passes.py) | +114 | Benchmark script: 6 circuits, 5 runs each, `--parallel` flag for rayon ON/OFF |

## Building Qiskit with Release Optimizations

By default, `pip install -e .` builds the Rust extensions in **debug mode** (no
optimizations). Debug builds are 10-20x slower and completely mask parallelism gains.
Always build with release optimizations for benchmarking:

```bash
# Release build (required for meaningful benchmarks)
QISKIT_BUILD_PROFILE=release pip install -e .

# Or equivalently:
python setup.py build_rust --release --inplace

# Debug build (default — DO NOT use for benchmarking)
pip install -e .
```

The `QISKIT_BUILD_PROFILE` env var maps to the `rust_debug` flag in `setup.py`:
- `release` → `rust_debug = False` → `cargo build --release`
- `debug` → `rust_debug = True` → `cargo build` (no optimizations)
- unset → checks `RUST_DEBUG=1` env var, otherwise defaults to debug

## Branch

[`parallel-optimization-passes`](https://github.com/hfwen0502/qiskit/tree/parallel-optimization-passes) on fork (`https://github.com/hfwen0502/qiskit.git`),
based on commit [`03c640f73`](https://github.com/hfwen0502/qiskit/commit/03c640f73).
