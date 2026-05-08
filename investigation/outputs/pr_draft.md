# PR: Replace FixedPoint loop exit with signal-based exit at Level 2

## Title
Replace FixedPoint with signal-based loop exit for Level 2 optimization

## Body

### Summary

Replace the Level 2 optimization loop's `FixedPoint` convergence check with a signal-based exit that tracks whether passes created new optimization opportunities. Eliminates the redundant confirmation iteration that FixedPoint requires.

### Approach

The loop continues if **any** signal fires:
1. **`_opt_pass_changed`** — multi-qubit gate removed (exposes longer 1Q runs)
2. **`_opt_1q_consolidated`** — rotations consolidated (shorter runs decompose better)
3. **`all_gates_in_basis == False`** — out-of-basis gate produced (needs re-optimization after BasisTranslator)

If none fires, re-running passes would produce identical results → exit.

### Results

Benchmarked on Intel Xeon Sapphire Rapids (160 vCPUs), release build, `seed_transpiler=42` for deterministic comparison. Backend: FakeTorino 133Q (device suites) and GenericBackendV2 127Q (custom sweep).

**Optimization loop speedup** (isolating the loop body, excluding layout/routing):

| Circuit | CZ Gates | Main (s) | Ours (s) | Speedup |
|---------|:--------:|:--------:|:--------:|:-------:|
| hwb12 | 191K | 5.84 | 3.97 | **1.47x (32% faster)** |
| vqe_uccsd_n28 | 207K | 23.86 | 19.67 | **1.21x (18% faster)** |

**End-to-end per-circuit improvements** (full `pm.run()`, larger circuits benefit most):

| Circuit | Suite | CZ Gates | Main (s) | Ours (s) | Speedup |
|---------|-------|:--------:|:--------:|:--------:|:-------:|
| hwb12 | feynman | 639K | 349.3 | 312.4 | **1.12x** |
| hwb11 | feynman | 335K | 179.2 | 160.5 | **1.12x** |
| clifford_100 | device_transpile | 66K | 23.6 | 20.9 | **1.13x** |
| vqe_uccsd_n28 | custom sweep | 207K | 26.3 | 22.8 | **1.15x** |
| hwb12 | custom sweep | 191K | 9.7 | 7.9 | **1.22x** |

Across all 81 tested circuits (59 from [Qiskit/benchpress](https://github.com/Qiskit/benchpress) + 22 custom), the aggregate speedup is **1.10x** (908.5s → 826.9s). End-to-end gains are lower than loop-body because layout/routing is unchanged and dominates total time for smaller circuits.

**Zero regressions**: All 81 circuits produce bit-identical 2Q gate count, total gate count, and depth.

We have not yet run the `abstract_transpile` suite from benchpress (QASMBench circuits across all-to-all, square, heavy-hex, and linear topologies). We can include those results if needed.

Detailed investigation: [optimization_loop.md](https://github.com/hfwen0502/qiskit/blob/pass-manager-investigation/investigation/docs/optimization_loop.md)

### Files Changed (6 files)

| File | Change |
|------|--------|
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Return `(bool, bool)` — `(multi_qubit_changed, rotations_consolidated)` |
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | Return `bool` (not used for loop control) |
| `crates/transpiler/src/passes/remove_identity_equiv.rs` | Return `bool` (true = multi-qubit identity removed) |
| `qiskit/transpiler/passes/optimization/commutative_cancellation.py` | Set `_opt_pass_changed` and `_opt_1q_consolidated` |
| `qiskit/transpiler/passes/optimization/remove_identity_equiv.py` | Set `_opt_pass_changed` |
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | Add `_optimization_check_changed_flag()`, use at Level 2 |

### Backward Compatibility

- ~80 lines changed across 6 files
- Rust return-type changes are additive; existing callers discard return values
- Level 3 unchanged (keeps `MinimumPoint` loop)
- All existing transpiler tests pass. Validated on 81 benchmark circuits with zero quality regressions.
