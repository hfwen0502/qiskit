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

![2Q gate count, main vs PR — every point on the diagonal](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/scatter_2q.png)

![Circuit depth, main vs PR — every point on the diagonal](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/scatter_depth.png)

**Compilation is faster.** Total suite compile time drops **−10.6% ± 0.3%** (5 runs), and
the PR is **faster in every group in every run**. Per group the reduction ranges **−6.7%
to −20.1%**.

![Per-circuit transpile time (mean of 5 runs), main vs PR](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/runtime_scatter.png)

![Per-group transpile time, mean ± stdev over 5 runs](https://raw.githubusercontent.com/hfwen0502/qiskit/pass-manager-investigation/investigation/outputs/pr16157_rebased/images/runtime_by_group.png)

In one line: **the PR helps ~43% of circuits, by ~13% each (median −13.5%), cutting total
suite compile time by 10.6%** — and the ~57% of circuits whose iteration count is unchanged
show **no slowdown** (mean Δ −2.6%, i.e. within run-to-run noise, if anything marginally
faster), confirming the added signal checks are cheap.

## Details — reproducing the data

All raw data, scripts, and plots are in this directory (see `REPRODUCE.md`):

- **Run / measure:** `scripts/run_group.sh` runs one group with `main` on NUMA socket 0
  and the PR on socket 1 concurrently; `scripts/run_all.sh` / the overnight harness repeat
  the full suite 5×. Per-group `main.json` / `pr.json` are pytest-benchmark output.
- **Iteration counts:** `scripts/iter_sweep_full.py` + `iter_sweep_ham.py` count loop
  iterations per circuit via a pass-execution callback (one `CommutativeCancellation`
  execution = one loop iteration); `scripts/iter_compare.py` produces the main-vs-PR
  distribution.
- **Benchpress recording patch:** `scripts/benchpress_patch.diff` adds total/1Q gate
  counts and 1Q/total depths to the recorder (benchpress records only the 2Q count by
  default) and seeds the abstract-group backend.
- **Plots:** `scripts/plot_pr_charts.py` regenerates every figure from the per-circuit TSVs.
- **The change itself:** `scripts/000{1,2,3}-*.patch` (the three commits, applied on
  current `main`).

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
