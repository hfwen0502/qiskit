"""Profile the optimization loop at level 2 and 3 to understand iteration behavior.

Instruments the DoWhileController loop in the optimization stage to measure:
- Number of iterations before convergence
- Per-pass wall time and gate count changes
- Whether MinimumPoint (L3) or FixedPoint (L2) triggers convergence
- Which passes actually contribute reductions

Compares level 2 vs level 3 side-by-side:
- Level 2: pre-loop (ConsolidateBlocks+UnitarySynthesis once), loop (4 passes), FixedPoint
- Level 3: loop (6 passes including ConsolidateBlocks+UnitarySynthesis), MinimumPoint

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_optimization_loop.py
"""

import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import (
    EfficientSU2,
    QFT,
    QuantumVolume,
)
from qiskit.transpiler import PassManager, PassManagerConfig
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.transpiler.passes import (
    ConsolidateBlocks,
    UnitarySynthesis,
    RemoveIdentityEquivalent,
    Optimize1qGatesDecomposition,
    CommutativeCancellation,
    ContractIdleWiresInControlFlow,
    GatesInBasis,
    Size,
    Depth,
)
from qiskit.transpiler.passes.utils.minimum_point import MinimumPoint
from qiskit.transpiler.passes.utils.fixed_point import FixedPoint
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit.passmanager import PropertySet
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Circuit builders (self-contained, no QASM files needed)
# ---------------------------------------------------------------------------

def build_qft_100():
    """100-qubit QFT — chain topology."""
    return QFT(100, name="QFT_100")


def build_qv_100():
    """100-qubit Quantum Volume — dense random."""
    return QuantumVolume(100, 100, seed=12345)


def build_efficientsu2_100():
    """100-qubit EfficientSU2 with linear entanglement — hardware-efficient ansatz."""
    return EfficientSU2(100, reps=3, entanglement="linear")


def build_qaoa_100():
    """100-qubit QAOA-like circuit — random ZZ interactions + mixer."""
    rng = np.random.default_rng(42)
    n = 100
    qc = QuantumCircuit(n)
    # Initial superposition
    for i in range(n):
        qc.h(i)
    # 3 QAOA layers
    for _ in range(3):
        # Problem unitary: random ZZ on ~3n edges (Barabasi-Albert-like density)
        for _ in range(3 * n):
            i, j = rng.choice(n, size=2, replace=False)
            angle = rng.uniform(0, np.pi)
            qc.rzz(angle, int(i), int(j))
        # Mixer
        for i in range(n):
            qc.rx(rng.uniform(0, np.pi), i)
    qc.measure_all()
    return qc


def build_bv_100():
    """100-qubit Bernstein-Vazirani for all-ones bitstring."""
    n = 100
    qc = QuantumCircuit(n, n - 1)
    qc.x(n - 1)
    qc.h(range(n))
    for i in range(n - 1):
        qc.cx(i, n - 1)
    qc.h(range(n))
    qc.measure(range(n - 1), range(n - 1))
    return qc


def build_heisenberg_100():
    """100-qubit square-lattice Heisenberg model (nearest-neighbor XX+YY+ZZ)."""
    n = 100
    side = 10  # 10x10 grid
    qc = QuantumCircuit(n)
    # Initial state
    for i in range(n):
        qc.h(i)
    # Trotter step: nearest-neighbor XX+YY+ZZ on 10x10 grid
    dt = 0.1
    for rep in range(3):
        for i in range(side):
            for j in range(side):
                q = i * side + j
                # Right neighbor
                if j + 1 < side:
                    r = i * side + (j + 1)
                    qc.rxx(dt, q, r)
                    qc.ryy(dt, q, r)
                    qc.rzz(dt, q, r)
                # Down neighbor
                if i + 1 < side:
                    d = (i + 1) * side + j
                    qc.rxx(dt, q, d)
                    qc.ryy(dt, q, d)
                    qc.rzz(dt, q, d)
    qc.measure_all()
    return qc


CIRCUITS = {
    "QFT_100": build_qft_100,
    "QV_100": build_qv_100,
    "EfficientSU2_100": build_efficientsu2_100,
    "QAOA_100": build_qaoa_100,
    "BV_100": build_bv_100,
    "Heisenberg_100": build_heisenberg_100,
}


# ---------------------------------------------------------------------------
# Instrumented optimization loop
# ---------------------------------------------------------------------------

def get_circuit_stats(dag):
    """Get gate count and depth from a DAG."""
    size = dag.size()
    depth = dag.depth()
    n2q = 0
    for node in dag.op_nodes():
        if len(node.qargs) == 2:
            n2q += 1
    return {"size": size, "depth": depth, "2q_gates": n2q}


def run_pass_instrumented(dag, pass_name, pass_inst, prop_set):
    """Run a single pass with timing and gate tracking."""
    pass_inst.property_set = prop_set
    stats_before = get_circuit_stats(dag)
    t_start = time.perf_counter()

    try:
        new_dag = pass_inst.run(dag)
        if new_dag is not None:
            dag = new_dag
    except Exception as e:
        return dag, {
            "name": pass_name,
            "time_ms": 0,
            "error": str(e),
        }

    t_elapsed = time.perf_counter() - t_start
    stats_after = get_circuit_stats(dag)

    return dag, {
        "name": pass_name,
        "time_ms": t_elapsed * 1000,
        "size_before": stats_before["size"],
        "size_after": stats_after["size"],
        "size_delta": stats_after["size"] - stats_before["size"],
        "2q_before": stats_before["2q_gates"],
        "2q_after": stats_after["2q_gates"],
        "2q_delta": stats_after["2q_gates"] - stats_before["2q_gates"],
        "depth_before": stats_before["depth"],
        "depth_after": stats_after["depth"],
        "depth_delta": stats_after["depth"] - stats_before["depth"],
    }


def profile_optimization_loop(circuit, backend, optimization_level=3):
    """Run transpilation and profile the optimization stage loop.

    Supports both level 2 (pre-loop + FixedPoint loop) and level 3
    (all-in-loop + MinimumPoint).

    Returns dict with per-iteration, per-pass timing and gate count data.
    """
    target = backend.target
    basis_gates = list(target.operation_names)

    # Step 1: Run init + layout + routing + translation (everything before optimization)
    pm_pre_opt = generate_preset_pass_manager(optimization_level, backend=backend)
    pm_pre_opt.optimization = PassManager()  # Empty — skip optimization

    t0 = time.perf_counter()
    pre_opt_circuit = pm_pre_opt.run(circuit)
    pre_opt_time = time.perf_counter() - t0

    dag = circuit_to_dag(pre_opt_circuit)
    pre_opt_stats = get_circuit_stats(dag)

    results = {
        "optimization_level": optimization_level,
        "pre_opt_time": pre_opt_time,
        "pre_opt_stats": pre_opt_stats,
        "pre_loop_passes": [],
        "iterations": [],
    }

    prop_set = PropertySet()
    gates_in_basis = GatesInBasis(basis_gates, target=target)

    # Build passes
    consolidate = ConsolidateBlocks(basis_gates=basis_gates, target=target)
    unitary_synth = UnitarySynthesis(basis_gates, target=target)
    remove_id = RemoveIdentityEquivalent(target=target)
    opt_1q = Optimize1qGatesDecomposition(basis=basis_gates, target=target)
    commutative = CommutativeCancellation(target=target)
    contract_idle = ContractIdleWiresInControlFlow()

    if optimization_level == 2:
        # Level 2: pre-loop runs ConsolidateBlocks + UnitarySynthesis once
        pre_loop_passes = [
            ("ConsolidateBlocks", consolidate),
            ("UnitarySynthesis", unitary_synth),
        ]
        loop_passes = [
            ("RemoveIdentityEquivalent", remove_id),
            ("Optimize1qGatesDecomposition", opt_1q),
            ("CommutativeCancellation", commutative),
            ("ContractIdleWiresInControlFlow", contract_idle),
        ]

        # Run pre-loop
        for pass_name, pass_inst in pre_loop_passes:
            dag, pass_data = run_pass_instrumented(dag, pass_name, pass_inst, prop_set)
            results["pre_loop_passes"].append(pass_data)

        # Convergence: FixedPoint
        size_pass = Size(recurse=True)
        depth_pass = Depth(recurse=True)
        fp_size = FixedPoint("size")
        fp_depth = FixedPoint("depth")

        max_iterations = 20
        for iteration in range(max_iterations):
            iter_data = {"iteration": iteration + 1, "passes": []}

            for pass_name, pass_inst in loop_passes:
                dag, pass_data = run_pass_instrumented(dag, pass_name, pass_inst, prop_set)
                iter_data["passes"].append(pass_data)

            # Run GatesInBasis check
            gates_in_basis.property_set = prop_set
            gates_in_basis.run(dag)
            needs_unroll = not prop_set.get("all_gates_in_basis", True)
            iter_data["needs_unroll"] = needs_unroll
            iter_data["ran_translation"] = False

            if needs_unroll:
                translation_pm = generate_preset_pass_manager(
                    optimization_level, backend=backend
                )
                trans_dag = circuit_to_dag(translation_pm.translation.run(dag_to_circuit(dag)))
                if trans_dag is not None:
                    dag = trans_dag
                iter_data["ran_translation"] = True

            # Run Size/Depth/FixedPoint
            for p in [size_pass, depth_pass, fp_size, fp_depth]:
                p.property_set = prop_set
                p.run(dag)

            iter_stats = get_circuit_stats(dag)
            iter_data["end_stats"] = iter_stats

            size_fp = prop_set.get("size_fixed_point", False)
            depth_fp = prop_set.get("depth_fixed_point", False)
            converged = size_fp and depth_fp
            iter_data["minimum_point_reached"] = converged

            results["iterations"].append(iter_data)

            if converged:
                results["converged_at"] = iteration + 1
                results["convergence_reason"] = "fixed_point"
                break
        else:
            results["converged_at"] = max_iterations
            results["convergence_reason"] = "max_iterations"

    elif optimization_level == 3:
        # Level 3: all passes inside the loop, MinimumPoint convergence
        loop_passes = [
            ("ConsolidateBlocks", consolidate),
            ("UnitarySynthesis", unitary_synth),
            ("RemoveIdentityEquivalent", remove_id),
            ("Optimize1qGatesDecomposition", opt_1q),
            ("CommutativeCancellation", commutative),
            ("ContractIdleWiresInControlFlow", contract_idle),
        ]

        size_pass = Size(recurse=True)
        depth_pass = Depth(recurse=True)
        min_point = MinimumPoint(["depth", "size"], "optimization_loop", backtrack_depth=5)

        max_iterations = 20
        for iteration in range(max_iterations):
            iter_data = {"iteration": iteration + 1, "passes": []}

            for pass_name, pass_inst in loop_passes:
                dag, pass_data = run_pass_instrumented(dag, pass_name, pass_inst, prop_set)
                iter_data["passes"].append(pass_data)

            # Run GatesInBasis check
            gates_in_basis.property_set = prop_set
            gates_in_basis.run(dag)
            needs_unroll = not prop_set.get("all_gates_in_basis", True)
            iter_data["needs_unroll"] = needs_unroll
            iter_data["ran_translation"] = False

            if needs_unroll:
                translation_pm = generate_preset_pass_manager(
                    optimization_level, backend=backend
                )
                trans_dag = circuit_to_dag(translation_pm.translation.run(dag_to_circuit(dag)))
                if trans_dag is not None:
                    dag = trans_dag
                iter_data["ran_translation"] = True

            # Run Size/Depth/MinimumPoint
            for p in [size_pass, depth_pass, min_point]:
                p.property_set = prop_set
                new_dag = p.run(dag)
                if new_dag is not None:
                    dag = new_dag

            iter_stats = get_circuit_stats(dag)
            iter_data["end_stats"] = iter_stats
            iter_data["minimum_point_reached"] = prop_set.get(
                "optimization_loop_minimum_point", False
            )

            results["iterations"].append(iter_data)

            if prop_set.get("optimization_loop_minimum_point", False):
                results["converged_at"] = iteration + 1
                results["convergence_reason"] = "minimum_point"
                break
        else:
            results["converged_at"] = max_iterations
            results["convergence_reason"] = "max_iterations"

    results["final_stats"] = get_circuit_stats(dag)
    return results


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def print_results(name, results):
    """Print profiling results for one circuit."""
    level = results["optimization_level"]
    print(f"\n{'=' * 80}")
    print(f"Circuit: {name} (Level {level})")
    print(f"{'=' * 80}")
    print(f"Pre-optimization: {results['pre_opt_time']*1000:.0f}ms "
          f"(size={results['pre_opt_stats']['size']}, "
          f"2q={results['pre_opt_stats']['2q_gates']}, "
          f"depth={results['pre_opt_stats']['depth']})")

    # Print pre-loop passes (Level 2)
    if results["pre_loop_passes"]:
        print(f"\nPre-loop passes:")
        for p in results["pre_loop_passes"]:
            if "error" in p:
                print(f"  {p['name']}: ERROR {p['error']}")
            else:
                print(f"  {p['name']}: {p['time_ms']:.1f}ms, "
                      f"2Q: {p['2q_before']}→{p['2q_after']} ({p['2q_delta']:+d})")

    print(f"Converged at iteration {results['converged_at']} ({results['convergence_reason']})")
    print(f"Final: size={results['final_stats']['size']}, "
          f"2q={results['final_stats']['2q_gates']}, "
          f"depth={results['final_stats']['depth']}")

    # Per-iteration summary
    conv_label = "FixPt?" if level == 2 else "MinPt?"
    print(f"\n{'Iter':>4s}  {'Size':>6s}  {'2Q':>6s}  {'Depth':>6s}  {'Time(ms)':>9s}  "
          f"{'Unroll?':>7s}  {conv_label:>6s}")
    print("-" * 55)
    for it in results["iterations"]:
        total_time = sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
        s = it["end_stats"]
        print(f"{it['iteration']:4d}  {s['size']:6d}  {s['2q_gates']:6d}  {s['depth']:6d}  "
              f"{total_time:9.1f}  {'YES' if it.get('ran_translation') else 'no':>7s}  "
              f"{'YES' if it.get('minimum_point_reached') else 'no':>6s}")

    # Per-pass cumulative time and total gate delta
    print(f"\nPer-pass cumulative stats (loop only):")
    print(f"{'Pass':>35s}  {'Total ms':>9s}  {'Avg ms':>8s}  "
          f"{'Total size delta':>16s}  {'Total 2Q delta':>15s}")
    print("-" * 90)
    if results["iterations"]:
        pass_names = [p["name"] for p in results["iterations"][0]["passes"]]
        for pname in pass_names:
            times = []
            size_deltas = []
            twoq_deltas = []
            for it in results["iterations"]:
                for p in it["passes"]:
                    if p["name"] == pname and "time_ms" in p:
                        times.append(p["time_ms"])
                        size_deltas.append(p.get("size_delta", 0))
                        twoq_deltas.append(p.get("2q_delta", 0))
            total_t = sum(times)
            avg_t = total_t / len(times) if times else 0
            total_sd = sum(size_deltas)
            total_2qd = sum(twoq_deltas)
            print(f"{pname:>35s}  {total_t:9.1f}  {avg_t:8.1f}  "
                  f"{total_sd:16d}  {total_2qd:15d}")


def get_total_opt_time(results):
    """Total optimization time including pre-loop and loop passes."""
    pre_loop_t = sum(p["time_ms"] for p in results["pre_loop_passes"] if "time_ms" in p)
    loop_t = sum(
        sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
        for it in results["iterations"]
    )
    return pre_loop_t + loop_t


def print_summary(all_results, level):
    """Print comparison summary across all circuits for one level."""
    print(f"\n{'=' * 80}")
    print(f"SUMMARY — Level {level}")
    print(f"{'=' * 80}")

    # Table 1: Loop Convergence & Timing
    print(f"\n--- Table 1: Loop Convergence & Timing ---\n")
    print(f"{'Circuit':>20s}  {'Iters':>5s}  {'Productive':>10s}  {'Why Stopped':>20s}  "
          f"{'Opt(ms)':>8s}  {'Pre-Opt(ms)':>11s}  {'Opt %':>6s}")
    print("-" * 90)
    for name, results in all_results.items():
        n_iters = results["converged_at"]
        opt_time = get_total_opt_time(results)
        pre_time = results["pre_opt_time"] * 1000
        opt_pct = (opt_time / (opt_time + pre_time) * 100) if (opt_time + pre_time) > 0 else 0

        productive = 0
        prev_2q = results["pre_opt_stats"]["2q_gates"]
        # Count pre-loop as productive if it changed 2Q
        if results["pre_loop_passes"]:
            after_preloop = results["pre_loop_passes"][-1].get("2q_after", prev_2q)
            if after_preloop != prev_2q:
                productive += 1
            prev_2q = after_preloop
        for it in results["iterations"]:
            cur_2q = it["end_stats"]["2q_gates"]
            if cur_2q != prev_2q:
                productive += 1
            prev_2q = cur_2q

        reason = results["convergence_reason"]
        if reason == "fixed_point":
            reason = f"fixed point (iter {n_iters})"
        elif reason == "minimum_point":
            if n_iters <= 3:
                reason = "fixed point (iter 1-2)"
            else:
                iter1_stats = results["iterations"][0]["end_stats"]
                final_stats = results["final_stats"]
                if (iter1_stats["2q_gates"] == final_stats["2q_gates"] and
                        iter1_stats["depth"] == final_stats["depth"]):
                    reason = "backtrack (oscillation)"
                else:
                    reason = f"backtrack (depth={n_iters})"

        print(f"{name:>20s}  {n_iters:5d}  {productive:10d}  {reason:>20s}  "
              f"{opt_time:8.0f}  {pre_time:11.0f}  {opt_pct:5.1f}%")

    # Table 2: Gate Quality Impact
    print(f"\n--- Table 2: Gate Quality (2Q Gates) ---\n")
    print(f"{'Circuit':>20s}  {'Pre-Opt':>8s}  {'After Iter1':>11s}  {'Final':>6s}  "
          f"{'Iter1 Reduction':>15s}  {'Loop Extra':>10s}  {'Total Reduction':>15s}")
    print("-" * 95)
    for name, results in all_results.items():
        pre_2q = results["pre_opt_stats"]["2q_gates"]
        # "After Iter 1" = after pre-loop (L2) or after first loop iter (L3)
        if results["pre_loop_passes"]:
            # Level 2: iter 1 work is the pre-loop
            iter1_2q = results["pre_loop_passes"][-1].get("2q_after", pre_2q)
        else:
            iter1_2q = results["iterations"][0]["end_stats"]["2q_gates"]
        final_2q = results["final_stats"]["2q_gates"]
        iter1_red = pre_2q - iter1_2q
        iter1_pct = (iter1_red / pre_2q * 100) if pre_2q > 0 else 0
        loop_extra = iter1_2q - final_2q
        total_red = pre_2q - final_2q
        total_pct = (total_red / pre_2q * 100) if pre_2q > 0 else 0
        print(f"{name:>20s}  {pre_2q:8d}  {iter1_2q:11d}  {final_2q:6d}  "
              f"{iter1_red:6d} ({iter1_pct:4.1f}%)  {loop_extra:10d}  "
              f"{total_red:6d} ({total_pct:4.1f}%)")


def print_comparison(l2_results, l3_results):
    """Print side-by-side comparison of Level 2 vs Level 3."""
    print(f"\n{'=' * 80}")
    print("COMPARISON — Level 2 vs Level 3")
    print(f"{'=' * 80}")

    # Convergence comparison
    print(f"\n--- Convergence ---\n")
    print(f"{'Circuit':>20s}  {'L2 Iters':>8s}  {'L3 Iters':>8s}  "
          f"{'L2 Opt(ms)':>10s}  {'L3 Opt(ms)':>10s}  {'Speedup':>8s}")
    print("-" * 75)
    for name in l2_results:
        r2 = l2_results[name]
        r3 = l3_results[name]
        t2 = get_total_opt_time(r2)
        t3 = get_total_opt_time(r3)
        speedup = t3 / t2 if t2 > 0 else float('inf')
        print(f"{name:>20s}  {r2['converged_at']:8d}  {r3['converged_at']:8d}  "
              f"{t2:10.0f}  {t3:10.0f}  {speedup:7.1f}x")

    # Gate quality comparison
    print(f"\n--- Gate Quality (Final 2Q Gates) ---\n")
    print(f"{'Circuit':>20s}  {'Pre-Opt':>8s}  {'L2 Final':>8s}  {'L3 Final':>8s}  "
          f"{'L2 Reduction':>12s}  {'L3 Reduction':>12s}  {'L3-L2 Diff':>10s}")
    print("-" * 90)
    for name in l2_results:
        r2 = l2_results[name]
        r3 = l3_results[name]
        pre_2q = r2["pre_opt_stats"]["2q_gates"]
        l2_final = r2["final_stats"]["2q_gates"]
        l3_final = r3["final_stats"]["2q_gates"]
        l2_red = pre_2q - l2_final
        l3_red = pre_2q - l3_final
        l2_pct = (l2_red / pre_2q * 100) if pre_2q > 0 else 0
        l3_pct = (l3_red / pre_2q * 100) if pre_2q > 0 else 0
        diff = l3_final - l2_final  # negative = L3 is better
        print(f"{name:>20s}  {pre_2q:8d}  {l2_final:8d}  {l3_final:8d}  "
              f"{l2_red:5d} ({l2_pct:4.1f}%)  {l3_red:5d} ({l3_pct:4.1f}%)  {diff:+10d}")

    # Total time comparison (pre-opt + opt)
    print(f"\n--- Total Transpile Time ---\n")
    print(f"{'Circuit':>20s}  {'L2 Total(ms)':>12s}  {'L3 Total(ms)':>12s}  {'L3/L2':>8s}")
    print("-" * 60)
    for name in l2_results:
        r2 = l2_results[name]
        r3 = l3_results[name]
        t2_total = r2["pre_opt_time"] * 1000 + get_total_opt_time(r2)
        t3_total = r3["pre_opt_time"] * 1000 + get_total_opt_time(r3)
        ratio = t3_total / t2_total if t2_total > 0 else float('inf')
        print(f"{name:>20s}  {t2_total:12.0f}  {t3_total:12.0f}  {ratio:7.2f}x")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Basis gates: {list(backend.target.operation_names)[:5]}...")

    l2_results = {}
    l3_results = {}

    for name, builder in CIRCUITS.items():
        print(f"\n{'=' * 80}")
        print(f"Building {name}...")
        t0 = time.perf_counter()
        circuit = builder()
        build_time = time.perf_counter() - t0
        print(f"  Built in {build_time*1000:.0f}ms: {circuit.num_qubits}Q, "
              f"{circuit.size()} gates")

        for level in [2, 3]:
            print(f"\n  Profiling Level {level}...")
            try:
                results = profile_optimization_loop(circuit, backend, optimization_level=level)
                if level == 2:
                    l2_results[name] = results
                else:
                    l3_results[name] = results
                print_results(name, results)
            except Exception as e:
                print(f"    ERROR: {e}")
                import traceback
                traceback.print_exc()

    # Per-level summaries
    print_summary(l2_results, level=2)
    print_summary(l3_results, level=3)

    # Side-by-side comparison
    print_comparison(l2_results, l3_results)


if __name__ == "__main__":
    main()
