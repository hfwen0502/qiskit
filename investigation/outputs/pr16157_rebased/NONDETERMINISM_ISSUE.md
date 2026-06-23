# (DRAFT) Non-determinism in `optimization_level=2`: 1Q gate count and circuit depth vary with a fixed PassManager and seed_transpiler

> Status: **draft for a future upstream Qiskit issue — not yet filed.** Pending another
> review pass. Supporting data in this directory: `recheck/` (8 runs of `qec_en_n5` on
> `main` alone) and the per-iteration sweep (`overnight_periter.tsv`, 5 runs/circuit on
> both `main` and the PR build).

### Summary

At `optimization_level=2`, running the **same `PassManager`** on the **same circuit**, with
`seed_transpiler` set explicitly, produces **different output across calls**. The
**2-qubit gate count is always stable**; the variation is confined to the **1-qubit
decomposition**, which surfaces as a differing **1Q-gate count** and/or **circuit depth**
(equivalent decompositions of the same unitary). So `pm.run()` is **not deterministic even
with a fixed seed**, and transpilation is not reproducible bit-for-bit.

### Cases observed (on `main` alone, across repeated runs)

Measured on `main` (`ea2546703`) by itself — none of these is caused by a PR:

| circuit | metric that varies | values seen | 2Q gate count |
|---|---|---|---|
| `qec_en_n5-square`         | 1Q gate count | 47 / 48 / 49 | 10 (stable) |
| `lpn_n5-heavy-hex`         | 1Q gate count | 18 / 19      | 2 (stable)  |
| `lpn_n5-heavy-hex`         | circuit depth | 8 / 10       | 2 (stable)  |
| `knn_n25-all-to-all`       | circuit depth | 226 / 227    | 84 (stable) |
| `swap_test_n25-all-to-all` | circuit depth | 228 / 229    | 84 (stable) |

(QASMBench circuits at `optimization_level=2`. The 2Q-gate count is identical on every run
for all of them — only the 1Q decomposition wobbles.)

### Steps to reproduce

`qec_en_n5` from QASMBench, compiled for a 9-qubit square target. The target here is built
with benchpress's `FlexibleBackend` (a `GenericBackendV2` with a seeded, error-weighted
model — the error weighting is what exposes the tie; see "Likely cause"):

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

1Q gate count varies (47/48/49); 2Q gate count is constant (10). The **same kind of
variation, in circuit depth**, appears on `lpn_n5-heavy-hex`, `knn_n25-all-to-all`, and
`swap_test_n25-all-to-all` (see the table) — all with the same `PassManager` and the same
`seed_transpiler`.

### Expected behavior

With a fixed pass manager and `seed_transpiler`, `pm.run()` on the same circuit should be
deterministic in gate counts and depth.

### Why this matters

- Transpilation is not reproducible even with `seed_transpiler` set.
- It complicates regression testing, result caching, and debugging: a 1Q-gate-count or depth
  "diff" between two runs or two branches may be pure non-determinism rather than a real
  change. (This surfaced while benchmarking a transpiler PR, where it produced spurious ±1
  1Q-gate and depth "regressions" on the five cases above.)

### Likely cause

Because it varies with a **fixed `PassManager` + `seed_transpiler` within a single process**,
this is neither the `QISKIT_TRANSPILER_SEED` env-parsing issue nor Python hash-seed
randomization (which is constant per process). It looks like an **error-weighted tie-break in
the 1Q-rotation decomposition** (`Optimize1qGatesDecomposition`) resolved by
**non-deterministic iteration order** — e.g. a Rust `HashMap`/`HashSet` whose `ahash` random
state is initialized fresh per `pm.run`, so the choice among near-equal-cost decompositions
changes each call. The chosen decomposition can differ in 1Q-gate count and/or depth, which
is why both metrics appear in the cases above. A plain `GenericBackendV2` with default error
rates does **not** trigger it; an error-weighted tie (as from a randomized-but-seeded error
model) does.

### Environment

- qiskit `main` @ `ea2546703` (2.6.0.dev0), Python 3.11, Linux x86_64.
