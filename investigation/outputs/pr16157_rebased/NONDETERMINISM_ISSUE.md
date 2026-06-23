# (DRAFT) Non-determinism in `optimization_level=2`: 1Q gate count varies with a fixed PassManager and seed_transpiler

> Status: **draft for a future upstream Qiskit issue — not yet filed.** Pending another
> review pass. Supporting data: `recheck/` (8 runs of qec_en_n5 on main alone, varying
> 1Q count) in this directory.

### Summary

At `optimization_level=2`, running the **same `PassManager`** on the **same circuit**, in a
**single process**, with `seed_transpiler` set explicitly, produces circuits with **different
1-qubit gate counts** across calls. The 2-qubit gate count and the depth are stable — only the
single-qubit decomposition differs between equivalent representations of the same unitary.

In other words, `pm.run()` is **not deterministic even with a fixed seed**, so transpilation is
not reproducible bit-for-bit.

### Steps to reproduce

`qec_en_n5` from QASMBench, compiled for a 9-qubit square target. The target here is built with
benchpress's `FlexibleBackend` (a `GenericBackendV2` with a seeded, error-weighted model — the
error weighting is what exposes the tie; see "Likely cause"):

```python
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from benchpress.utilities.backends import FlexibleBackend
from benchpress.workouts.abstract_transpile.qasmbench import SMALL_CIRC_TOPO, SMALL_NAMES

i = SMALL_NAMES.index("qec_en_n5-square")
qasm, topo = SMALL_CIRC_TOPO[i]
circ = QuantumCircuit.from_qasm_file(qasm)

backend = FlexibleBackend(circ.num_qubits, topo, control_flow=True)          # built ONCE
pm = generate_preset_pass_manager(optimization_level=2, backend=backend,
                                  seed_transpiler=42)                         # built ONCE

def oneq(c):
    ops = c.count_ops()
    return sum(ops.values()) - sum(v for k, v in ops.items() if k in ("cz", "cx", "ecr"))

print([oneq(pm.run(circ)) for _ in range(10)])
```

### Actual behavior

```
[47, 47, 48, 49, 48, 49, 47, 48, 47, 47]
```

1Q gate count varies between 47/48/49; 2Q gate count is constant (10) and depth is constant
across all runs. Same process, same pass manager, same `seed_transpiler`.

### Expected behavior

With a fixed pass manager and `seed_transpiler`, `pm.run()` on the same circuit should be
deterministic.

### Why this matters

- Transpilation is not reproducible even with `seed_transpiler` set.
- It complicates regression testing, result caching, and debugging: a 1Q-gate-count "diff"
  between two runs or two branches may be pure non-determinism rather than a real change. (This
  surfaced while benchmarking a transpiler PR, where it produced a spurious +1 1Q-gate "regression".)

### Likely cause

Because it varies with a **fixed `PassManager` + `seed_transpiler` within a single process**, this
is neither the `QISKIT_TRANSPILER_SEED` env-parsing issue nor Python hash-seed randomization (which
is constant per process). It looks like an **error-weighted tie-break in the 1Q-rotation
decomposition** (`Optimize1qGatesDecomposition`) being resolved by **non-deterministic iteration
order** — e.g. a Rust `HashMap`/`HashSet` whose `ahash` random state is initialized fresh per
`pm.run`, so iteration order (and thus the choice among near-equal-cost decompositions) changes each
call. A plain `GenericBackendV2` with default error rates does **not** trigger it; an error-weighted
tie (as produced by a randomized-but-seeded error model) does.

### Environment

- qiskit `main` @ `ea2546703` (2.6.0.dev0), Python 3.11, Linux x86_64.
