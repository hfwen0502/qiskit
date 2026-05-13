---
marp: true
theme: default
paginate: true
size: 16:9
header: 'Smarter Loop Exit for Level 2 Optimization'
footer: 'Sophia Wen | IBM Quantum | qiskit#16157'
style: |
  section { font-size: 22px; }
  h1 { color: #0530AD; }
  h2 { color: #0530AD; }
  table { font-size: 18px; }
  pre { font-size: 16px; }
  code { font-size: 16px; }
---

# Smarter Loop Exit for Level 2 Optimization

**Replace `FixedPoint` with three opportunity-producing signals**

Sophia Wen — IBM Quantum

PR: [qiskit#16157](https://github.com/Qiskit/qiskit/pull/16157)
Branch: `pass-manager-investigation` on `hfwen0502/qiskit`

*Scope: Level 2 optimization loop only. Level 3 unchanged.*

---

## Where in the transpiler?

```
Input circuit (abstract)
    │
    ▼  1. INIT          — high-level optimization on abstract circuit
    ▼  2. LAYOUT        — virtual → physical qubit mapping
    ▼  3. ROUTING       — insert SWAPs to satisfy connectivity
    ▼  4. TRANSLATION   — convert to hardware basis gates
    ▼  5. OPTIMIZATION  ← we are here   (a loop of passes)
    ▼  6. SCHEDULING    — timing / delays
    ▼
Output circuit (hardware-ready)
```

We change **only stage 5**, and only at **optimization level 2**.
Stages 1–4, 6, and Level-3 stage 5 are untouched.

---

## What the Level 2 optimization stage does

After routing + translation, the circuit is hardware-legal but messy:

- Routing inserted SWAPs that may now be redundant after gate cancellations.
- Translation rewrote abstract gates as basis-gate sequences that may simplify.
- Local 1Q runs created by both stages can often be combined into shorter sequences.

The optimization stage is a **loop of passes** that simplify gate by gate:

- Each pass scans the DAG and rewrites local patterns.
- Some passes create new opportunities for others (e.g., removing a 2Q gate exposes a longer 1Q run that the next 1Q pass can recombine).
- The loop repeats until no more progress is possible — a **convergence check** decides when to stop.

The convergence check is what this PR changes.

---

## The Level 2 optimization pipeline (the loop body)

Each iteration runs these passes, in order:

```
Optimize1qGatesDecomposition
CommutativeInverseCancellation       (also runs in init)
RemoveIdentityEquivalent
ConsolidateBlocks                    (idle-wire contraction here, not block synthesis)
CommutativeCancellation
GatesInBasis  ──┐
                │  if False: BasisTranslator → Optimize1qGatesDecomposition
                ▼
Convergence check  ──── re-run loop / exit
```

Today's check: **`FixedPoint("depth", "size")`** — keeps re-running the loop
until both metrics are unchanged for a full iteration.

---

## Pass 1 — `Optimize1qGatesDecomposition`

Combines runs of consecutive 1Q gates on the same qubit into a single
canonical Euler decomposition, choosing the form with the **fewest gates**
(or lowest error). Always rebuilds the run; idempotent on stable input.

```
Before                      After (target basis: sx, x, rz)

q0 ──H──S──RZ(π/4)──         q0 ──RZ──SX──RZ──
       (3 gates)                  (3 gates, but only physical SX once)

q0 ──H──H──                  q0 ──            (cancels to identity)
       (2 gates)
```

**Key property**: stateless across calls. Given the same input run, it
produces the same canonical output. Re-running it without changing the
runs around it cannot find new optimizations.

---

## Pass 2 — `CommutativeInverseCancellation`

Removes adjacent inverse pairs of gates: `H·H`, `X·X`, `CX·CX`,
`S·Sdg`, etc. Runs in **init** as well as in the L2 loop.

```
Before                       After

q0 ──H──H──●──               q0 ──●──
            │                       │
q1 ─────────⊕──CX──⊕──       q1 ───⊕──
                              (CX·CX cancelled)
```

**Effect**: removes pairs but doesn't create new pairs. Re-running on a
DAG it just simplified produces the same DAG.

---

## Pass 3 — `RemoveIdentityEquivalent`

Removes gates whose effect is approximately identity within an error
threshold (e.g., `RZ(θ)` with `θ ≈ 0`, or 2Q rotations close to 0/2π).

```
Before                        After

q0 ──RZ(1e-9)──RX(2π)──       q0 ──            (both ≈ I)

q0 ──●──                       q0 ──            (RZZ(2π) ≈ I, both qubits idle)
     │RZZ(2π)
q1 ──●──                       q1 ──
```

**Important for our work**: when this pass removes a **multi-qubit**
identity gate, two 1Q runs on either side merge into one — that's a new
optimization opportunity for `Optimize1qGatesDecomposition`.

---

## Pass 4 — `ConsolidateBlocks` (idle-wire contraction)

In the L2 loop, this is the **idle-wire contraction** form: collapses
adjacent blocks of gates that act on the same qubits, removing
unnecessary structure. The "block synthesis" form runs earlier.

```
Before                              After

q0 ──[block A]──[block B]──         q0 ──[A∘B fused]──
       │           │                       │
q1 ────●───────────●─────           q1 ────●──────────
```

Effect on us: rarely produces new opportunities by itself, but
prepares the DAG for `CommutativeCancellation` to find them.

---

## Pass 5 — `CommutativeCancellation`

Two effects:

**(a) Cancels gates across commutation boundaries.** A gate on a qubit can be
moved past commuting gates to find a partner to cancel with.

```
Before                              After

q0 ──H──Z──H──         q0 ──X──         (HZH = X)

q0 ──RZ(π/4)──●──RZ(-π/4)──   q0 ──●──
              │                       │
q1 ───────────⊕────────       q1 ────⊕──
        (RZs commute past CX, cancel)
```

**(b) Consolidates same-axis rotations.** `RZ(a) · RZ(b)` →
`RZ(a+b)`; same for RX. The merged angle may end up out-of-basis
(triggers Signal 3, see later) or zero (then identity-removed next pass).

This pass is the **biggest source of new optimization opportunities** in
the loop.

---

## Pass 6 — `GatesInBasis` → `BasisTranslator`

`GatesInBasis` is an analysis pass: it sets a property
`all_gates_in_basis = True / False` based on whether every gate in the DAG
is in the target basis.

If `False`, the loop runs `BasisTranslator` to rewrite the offending
gates, then `Optimize1qGatesDecomposition` to clean up the (typically
unoptimized) translation output.

```
After CommutativeCancellation merged two RZ rotations, the DAG might
contain RZ(θ) where θ is a value not directly representable in the
target's discrete RZ set.

  basis = [sx, x, rz, cz]                 → all in basis
  basis = [sx, x, rz(π/2), cz, rzz]       → some RZ(θ) need translation
```

This is the **third mechanism** by which the loop finds more work to do.

---

## Today's convergence check: `FixedPoint`

```python
# Stock Qiskit, Level 2:
loop = [Optimize1q, CommInvCancel, RemoveIdEquiv,
        Consolidate, CommCancel, GatesInBasis, ...]
loop_check = FixedPoint("depth", "size")
do_while_property_set("depth", "size") changes:
    run loop
    run loop_check        # measures depth & size after this iteration
```

Behavior:

1. Run the full loop, measure (depth, size).
2. Run it again, measure again. Did either change? If yes → loop.
3. If both unchanged for a full iteration → exit.

**The cost**: even after the passes have nothing left to do, FixedPoint
forces **one more full iteration** just to confirm. On large circuits
that confirmation iteration is expensive.

---

## What we change

Instead of measuring (depth, size) after the fact, **let the passes
themselves report whether they created an opportunity for the next
iteration**.

Three orthogonal boolean signals, set by passes during their normal run:

| Signal | Meaning | Set by |
|---|---|---|
| `_opt_pass_changed` | A multi-qubit gate was removed | `RemoveIdentityEquivalent`, `CommutativeCancellation` |
| `_opt_1q_consolidated` | Adjacent rotations merged | `CommutativeCancellation` |
| `all_gates_in_basis == False` | Out-of-basis gate produced | `GatesInBasis` (already present) |

**Loop continuation rule**: re-iterate iff *any* signal fired. If none
fired, the next iteration would produce the same DAG — exit immediately.

---

## Signal 1 — multi-qubit gate removed

When `RemoveIdentityEquivalent` or `CommutativeCancellation` deletes a 2Q
gate, the 1Q runs on either side merge into a longer one:

```
Before pass                      After pass

q0 ──[1Q run a]──●──[1Q run b]── ──→ q0 ──[1Q run a+b]──
                  │ removed
q1 ──...──────────●──...─────── ──→ q1 ──...──────────...
```

The longer run gives `Optimize1qGatesDecomposition` new material — the
canonical Euler form of `(a+b)` may have fewer gates than the canonical
forms of `a` and `b` individually.

→ Re-run the loop.

---

## Signal 2 — rotations consolidated

When `CommutativeCancellation` merges adjacent same-axis rotations:

```
Before                              After

q0 ──RZ(α)──[gate that commutes with RZ]──RZ(β)──
                                  ↓
q0 ──[gate]──RZ(α+β)──
```

The 1Q run that contained `RZ(α)` and `RZ(β)` is now **shorter** by one
gate. `Optimize1qGatesDecomposition` had previously canonicalized the
longer form; it can now do better on the shorter one.

→ Re-run the loop.

(Idempotent: only fires once. The next iteration's
`CommutativeCancellation` finds nothing new to merge.)

---

## Signal 3 — out-of-basis gate produced

`CommutativeCancellation` can produce a gate outside the target basis
(typically a non-canonical rotation angle). `GatesInBasis` detects this:

```
basis = [sx, x, rz, cz]

After CommutativeCancellation:
q0 ──RZ(π/4)──     all_gates_in_basis = True   → done

After CommutativeCancellation (different circuit):
q0 ──S──Sdg──H──   all_gates_in_basis = False  (S, Sdg not in basis)
                    → BasisTranslator runs, emits unoptimized sequence
                    → Optimize1qGatesDecomposition needs another pass
```

→ Re-run the loop.

(This signal is the existing `GatesInBasis` analysis — no new code
needed; it just becomes a loop-exit trigger.)

---

## Why the three signals are sufficient

Inside the L2 loop, a new optimization opportunity for the next
iteration can arise **only** through one of these three mechanisms:

1. A multi-qubit gate is removed → 1Q runs on either side merge (Signal 1).
2. Same-axis rotations collapse → 1Q run shortens (Signal 2).
3. An out-of-basis gate is produced → BasisTranslator emits an
   unoptimized sequence (Signal 3).

Other passes in the loop (`Optimize1qGatesDecomposition`,
`CommutativeInverseCancellation`, `ConsolidateBlocks`) are **stateless
on stable input**: re-running them without one of the three mechanisms
firing produces the same DAG.

If none of the three signals fires in an iteration, re-running the loop
is **provably a no-op** → exit immediately.

This is what FixedPoint discovers empirically by running an extra
iteration. We discover it directly.

---

## Code change footprint

**6 files, +69 / −11 lines.** Level 3 unchanged.

| File | Change |
|---|---|
| `crates/transpiler/src/passes/commutation_cancellation.rs` | Returns `(bool, bool)` for Signals 1 + 2 |
| `crates/transpiler/src/passes/remove_identity_equiv.rs` | Returns `bool` for Signal 1 |
| `crates/transpiler/src/passes/optimize_1q_gates_decomposition.rs` | Returns `bool` (informational; not used as signal) |
| `qiskit/transpiler/passes/optimization/commutative_cancellation.py` | Sets `_opt_pass_changed` + `_opt_1q_consolidated` |
| `qiskit/transpiler/passes/optimization/remove_identity_equiv.py` | Sets `_opt_pass_changed` |
| `qiskit/transpiler/preset_passmanagers/builtin_plugins.py` | New `_optimization_check_changed_flag()` replaces `FixedPoint` at L2 |

Signal 3 reuses the existing `GatesInBasis` analysis; no new code.

---

## Methodology for evidence

- **Suite**: full `benchpress/qiskit_gym/` transpile suite at
  `optimization_level=2`, **1,023 circuits** across 6 groups.
- **Builds**: Qiskit main `c25216340` vs PR `5e077757e` (rebased onto
  main), both release + mimalloc.
- **Seed**: `QISKIT_TRANSPILER_SEED=1`. (`=42` would resolve to 1 anyway
  due to a separate upstream walrus-precedence bug.)
- **Hardware**: 2-socket Intel SPR (40c × 2 threads each socket).
  Main on NUMA 0, PR on NUMA 1, parallel. NUMA sweep validated single-
  socket pinning is within 1.7% of full-machine on hwb12.
- **Benchpress patch**: 2-line diff adds total/1Q gate count + total/1Q
  depth alongside existing 2Q metrics; FlexibleBackend gets fixed
  `seed=12345` so abstract tests see deterministic backend targets.
- **Bug-2 exclusion**: 2 circuits (1 in device_hamiltonians, 1 in
  abstract_hamiltonians) hit a pre-existing upstream non-determinism
  (1Q rotation tie-break). 2Q counts identical on both, only 1Q drift.
  Excluded from aggregate; reproducer included.

---

## Per-group results (1,021 circuits, all bit-identical)

| Group | Circuits | 2Q count Δ | Total count Δ | Total depth Δ | Wall-clock |
|---|---:|---:|---:|---:|---:|
| feynman | 50 | 0 | 0 | 0 | **−22.7%** |
| device_hamiltonians | 80 | 0 | 0 | 0 | **−17.5%** |
| abstract_small | 168 | 0 | 0 | 0 | **−9.5%** |
| abstract_medium | 92 | 0 | 0 | 0 | **−19.1%** |
| abstract_large | 232 | 0 | 0 | 0 | **−17.5%** |
| abstract_hamiltonians | 399 | 0 | 0 | 0 | **−10.3%** |
| **Total** | **1,021** | **0** | **0** | **0** | **−16.2%** |

**Zero delta on every gate / depth metric across every group.**
Aggregate −16.2% wall-clock, range −9.5% to −22.7%, no slowdowns >5%.

---

## Headline speedups (per-circuit, end-to-end `pm.run()`)

| Circuit | Group | Main | PR | Speedup |
|---|---|---:|---:|---:|
| `hwb12` | feynman | 14.40 s | 10.85 s | **−24.7%** |
| `hwb11` | feynman | 6.76 s | 5.19 s | −23.2% |
| `100-linear` | abstract_large | 6.39 s | 4.68 s | **−26.7%** |
| `vqe_uccsd_n28-square` | abstract_large | 18.09 s | 13.62 s | −24.7% |
| `bwt_n37-linear` | abstract_large | 67.38 s | 50.81 s | −24.6% |
| `multiplier_n400-linear` | abstract_large | 26.24 s | 19.91 s | −24.1% |
| `factor247_n15-heavy-hex` | abstract_medium | 14.33 s | 11.03 s | −23.0% |
| `ham_gnp-k_5_n-90_rinst-02` | abstract_hams (linear) | 0.75 s | 0.55 s | −26.4% |

Wall-clock is **end-to-end** transpile (pipeline-wide); loop-only
speedup is larger but not directly measured by these runs.

---

## Stress test: hwb12

`hwb12.qasm` (Hidden Weighted Bit, 12 qubits, ~190K input gates) is
Matthew's go-to stress test for the L2 loop because the loop processes
**>1M gates** after pre-loop block consolidation.

| | Main | PR | Δ |
|---|---:|---:|---:|
| End-to-end transpile | 14.40 s | 10.85 s | **−24.7%** |
| 2Q gate count | 644,633 | 644,633 | **0** |
| 1Q gate count | 1,752,972 | 1,752,972 | **0** |
| Total depth | 1,074,718 | 1,074,718 | **0** |

**Bit-identical output on every metric**, end-to-end **−24.7%**.

Earlier loop-only measurement: **+47% / −32% wall-clock** (4.0s vs 5.8s
loop-body), confirming the wall-clock win is the loop, not noise.

---

## Two upstream Qiskit issues found (not caused by this PR)

### Bug 1 — walrus precedence in `QISKIT_TRANSPILER_SEED` parsing

```python
# Current (buggy) — `:=` binds looser than `is not None`
if seed := os.getenv("QISKIT_TRANSPILER_SEED", None) is not None:
    seed_transpiler = int(seed)
# Result: seed_transpiler is always 0 or 1 regardless of env-var value.

# Fix:
if (seed := os.getenv("QISKIT_TRANSPILER_SEED", None)) is not None:
    seed_transpiler = int(seed)
```

Pure-Python reproducer in evidence tarball. Will be filed upstream
separately.

### Bug 2 — unseeded RNG in 1Q rotation tie-break

2 of 1,023 circuits give different 1Q decompositions across fresh
Python subprocesses, even with `seed_transpiler` pinned. 2Q count
unchanged on both. Reproducer in tarball.

Both bugs predate this PR. Excluded from analysis to avoid distorting
results.

---

## Status

**PR #16157**: open, ready for re-review.

| Gate | Status |
|---|---|
| PR template + AI/LLM disclosure | ✅ |
| CLA signed | ✅ |
| Commit authorship | ✅ |
| Rebased on current main, no merge conflicts | ✅ |
| Evidence — full benchpress + raw JSONs + scripts attached as tarball | ✅ |
| Per-group + total no-regression on 6 metrics | ✅ |
| Wall-clock speedup data | ✅ |
| Re-review requested | ✅ |

**Next**: respond to any further review comments; file Bugs 1 and 2 as
separate issues; prep follow-up PRs if Matthew wants the same shape at
Level 3.
