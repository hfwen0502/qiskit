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

Benchmarked on Intel Xeon Sapphire Rapids (160 vCPUs), release build, `seed_transpiler=42`.

**Loop body improvement**: 11.27s → 8.85s (**22% faster**) on representative circuits.

**End-to-end (full `pm.run()`)** — the loop savings are diluted by fixed costs (SABRE layout/routing = ~69% of total time):

| Suite | Backend | Circuits | Main (s) | Ours (s) | Speedup | 2Q Gate Δ |
|-------|---------|:--------:|:--------:|:--------:|:-------:|:---------:|
| device_transpile | FakeTorino 133Q | 9 | 178.4 | 171.2 | **1.04x** | 0 |
| device_feynman | FakeTorino 133Q | 50 | 635.5 | 570.3 | **1.11x** | 0 |
| Custom sweep | GenericBackendV2 127Q | 22 | 94.6 | 85.4 | **1.11x** | 0 |

**Zero regressions**: All 81 circuits produce bit-identical gate counts (total, 2Q, 1Q) and depth.

Detailed investigation: [optimization_loop.md](investigation/docs/optimization_loop.md)

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

- Rust return-type changes are additive; existing callers discard return values
- Level 3 unchanged (keeps `MinimumPoint` loop)
- All 152 existing pass tests pass
