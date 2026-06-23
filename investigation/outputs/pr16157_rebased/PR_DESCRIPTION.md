# Replace the Level-2 optimization loop's fixed-point check with direct progress signals

## The problem

At `optimization_level=2`, the transpiler runs a small group of peephole passes in a
loop — 1Q-rotation decomposition, commutative cancellation, identity removal — repeating
until the circuit stops improving. Today that "stop" is decided by a `FixedPoint` on
circuit **depth and size**: the loop continues until both are unchanged across **two
consecutive** iterations.

The two-in-a-row rule means that whenever the loop *does* converge, it pays for **one
extra iteration that does no useful work**: after the last iteration that actually
improves the circuit, the loop must run the whole pass group once more, observe that
nothing changed, and only then declare convergence. That confirmation pass scales with
circuit size.

## The solution

Don't *infer* convergence from "the metrics stopped moving" — ask each pass whether it
did something a later pass could build on. If, in an iteration, **no pass created a new
optimization opportunity**, the circuit is already at a fixed point and the loop can stop
immediately, with no confirmation iteration.

The exit condition is no longer `FixedPoint("size","depth")`; the passes raise three
boolean signals and the loop continues only if at least one fired. Each signal is an
event that can expose further optimization next iteration — and, crucially, their
**absence is a sufficient condition for convergence**, so exiting early produces the
*same circuit* as the fixed-point loop:

1. **A 2-qubit gate was removed** (`CommutativeCancellation` cancelled a CX/CZ pair, or
   `RemoveIdentityEquivalent` dropped a near-identity 2Q gate) — changes adjacency, can
   expose new commutations/cancellations.
2. **1-qubit rotations were consolidated** (`Optimize1qGatesDecomposition` merged
   same-axis rotations) — reshapes 1Q runs, can open further merges. *Needed for
   correctness:* with a 2Q-only signal, a few circuits (QFT, QAOA) exited one iteration
   early and kept un-merged 1Q rotations; this signal closes that gap.
3. **Out-of-basis gates were produced** (`CommutativeCancellation` can emit e.g. an `RX`;
   `GatesInBasis` then flags the circuit and `BasisTranslator` runs in-loop) — a fresh
   translation yields unoptimized sequences the peephole passes can still clean up.
   *(Added after review.)*

**Scope:** `optimization_level=2` only. Levels 1 and 3 are unchanged.

## Summary

Benchmarked on the full [Benchpress](https://github.com/Qiskit/benchpress) transpile
suite — **1,023 circuits** (feynman, device-Hamiltonian, abstract QASMBench, abstract
Hamiltonian) — comparing current `main` against `main` + this PR, both built
release+mimalloc, run on a 2-socket NUMA host with baseline and PR pinned to separate
sockets, `QISKIT_TRANSPILER_SEED=1`. Timing is the mean of **5 full-suite runs**.

**Fewer loop iterations, never more.** ~**43% of circuits (435 / 1,021) compile with one
fewer optimization-loop iteration**; the rest keep the same count; **none take more**
(every delta is exactly +1 or 0). The effect concentrates in larger circuits (e.g.
abstract Hamiltonians: 249/400).

![Per-circuit change in Level-2 loop iterations (PR − main), colored by group](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/iteration_delta.png)

**Output is unchanged.** Across all 1,023 circuits and all 5 runs, **2Q-gate counts and
circuit depths are bit-identical** between `main` and the PR — every point on the diagonal.
The **1Q-gate count** is identical too, except on four circuits affected by a pre-existing,
PR-independent tie-break non-determinism (discussed in **Issues** below).

![2Q gate count, main vs PR — every point on the diagonal](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/scatter_2q.png)

![Circuit depth, main vs PR — every point on the diagonal](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/scatter_depth.png)

**Compilation is faster.** Total suite compile time drops **−10.6% ± 0.3%** (5 runs), and
the PR is **faster in every group in every run**. Per group the reduction ranges **−6.7%
to −20.1%**. A few circuits (**red rings** in the per-circuit plot) have PR's *mean*
slightly above main's — these are **within run-to-run noise**: on those circuits `main`
happened to have higher variance (a couple unusually fast runs pulling its mean down)
while the PR was *tighter* (lower stdev), and the gap is smaller than `main`'s own spread.
None is a real slowdown.

![Per-circuit transpile time (mean of 5 runs), main vs PR](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/runtime_scatter.png)

![Per-group transpile time, mean ± stdev over 5 runs](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/runtime_by_group.png)

In one line: **the PR helps ~43% of circuits, by ~13% each (median −13.5%), cutting total
suite compile time by 10.6%** — and the ~57% of circuits whose iteration count is unchanged
show **no slowdown** (mean Δ −2.6%, i.e. within run-to-run noise, if anything marginally
faster), confirming the added signal checks are cheap.

## Details — how the metrics are captured

All raw data, scripts, and plots are in this directory (see `REPRODUCE.md`). The harness
runs `main` (NUMA socket 0) and the PR (socket 1) **concurrently** per group via
`scripts/run_group.sh`, repeated 5× over the full suite by `scripts/overnight_suite.sh`;
each run writes pytest-benchmark `main.json` / `pr.json`.

**Transpile time** is the wall-clock of the single `pm.run(circuit)` call, timed by
pytest-benchmark (this is the benchpress transpile test — `scripts/run_group.sh` invokes it):

```python
pm = generate_preset_pass_manager(optimization_level=2, backend=backend)
@benchmark                       # pytest-benchmark times exactly this call
def result():
    return pm.run(circuit)
```

**Gate counts and depth** are recorded from the transpiled circuit by benchpress's output
recorder. Benchpress only records the 2Q count by default, so `scripts/benchpress_patch.diff`
extends it to 1Q / total counts and 1Q / total depth:

```python
ops = circuit.count_ops()
benchmark.extra_info["output_gate_count_2q"]    = ops.get(two_q_gate, 0)
benchmark.extra_info["output_gate_count_1q"]    = sum(ops.values()) - ops.get(two_q_gate, 0)
benchmark.extra_info["output_gate_count_total"] = sum(ops.values())
benchmark.extra_info["output_depth_2q"]    = circuit.depth(lambda i: i.operation.name == two_q_gate)
benchmark.extra_info["output_depth_1q"]    = circuit.depth(lambda i: len(i.qubits) == 1)
benchmark.extra_info["output_depth_total"] = circuit.depth()
```

**Loop-iteration count** is captured non-invasively with a `pm.run` callback that counts
executions of a loop-body pass — one `CommutativeCancellation` execution = one Level-2
optimization-loop iteration (`scripts/iter_sweep_full.py`, `iter_sweep_ham.py`):

```python
pm = generate_preset_pass_manager(2, backend, seed_transpiler=1)
n = 0
def callback(pass_, **_):        # called once per pass execution
    nonlocal n
    if type(pass_).__name__ == "CommutativeCancellation":
        n += 1
pm.run(circuit, callback=callback)   # n == number of loop iterations
```

`scripts/iter_compare.py` produces the main-vs-PR iteration distribution; `scripts/plot_pr_charts.py`
and `scripts/plot_iter_delta.py` regenerate every figure from the per-circuit TSVs
(`overnight_periter.tsv`, `iters_main_full.tsv`, `iters_pr_full.tsv`). The change itself is
`scripts/000{1,2,3}-*.patch` (the three commits on current `main`).

### How the speedup numbers are computed

Let `t(c)` be the mean `pm.run` wall-clock over the 5 runs for circuit `c`. The headline
(per group, and overall) is the **wall-clock-weighted total**, summed over *all* `N`
circuits in the set — nothing is excluded:

```
speedup = ( Σ_c t_PR(c) − Σ_c t_main(c) ) / Σ_c t_main(c)
```

All circuits count, but the ~57% with unchanged iteration count have `t_PR(c) ≈ t_main(c)`
so they contribute ≈0 to the numerator; the savers (mostly the large circuits) drive the
reduction. This is the honest "total time to transpile the suite" number: **−10.6% ± 0.3%**.

To separate *effect size* from *coverage*, we also report the unweighted per-circuit mean
of `(t_PR(c) − t_main(c)) / t_main(c)`:

```
savers (one fewer iteration):    mean −12.7%  (median −13.5%)   ← effect size where it acts
unchanged-iteration circuits:    mean  −2.6%                    ← ≈0, i.e. no slowdown
```

i.e. **the PR helps ~43% of circuits by ~13% each, which cuts total suite compile time by
10.6%**, and the rest are not slowed.

## Issues — pre-existing 1Q non-determinism (not introduced here)

A handful of circuits (4 of 1,023: `qec_en_n5-square`, `lpn_n5-heavy-hex`,
`knn_n25-all-to-all`, `swap_test_n25-all-to-all`) show **1Q-gate-count variation across
runs** in the 1Q scatter (`scatter_1q.png`) — on **both** `main` and the PR, independent
of this change. This is a pre-existing, seed-independent tie-break in 1Q-rotation
decomposition (it varies even with a fixed `PassManager` and `seed_transpiler`; verified
on `main` alone — see `recheck/` and `NONDETERMINISM_ISSUE.md`). The 2Q-gate count and
depth are unaffected. It is called out here only so the off-diagonal points in the 1Q
plot are not mistaken for a PR-induced change.

![1Q gate count, main vs PR — only the 4 non-determinism circuits leave the diagonal](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/scatter_1q.png)

The red-ringed off-diagonal points above come entirely from those four circuits; every
other point is on the diagonal. This non-determinism is independent of this PR, so we will
**open a separate PR** to address it (root-cause analysis in `NONDETERMINISM_ISSUE.md`).

## AI / LLM disclosure

- [x] I used **Claude Code (Claude Opus)** to help with the rebase onto current `main` and
  the conflict resolution in the parallelized 1Q-decomposition pass, the benchmark
  orchestration/analysis, and this write-up. A human reviewed every line.
