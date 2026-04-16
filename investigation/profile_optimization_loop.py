"""Profile the optimization loop at level 3 to understand iteration behavior.

Instruments the DoWhileController loop in the optimization stage to measure:
- Number of iterations before convergence
- Per-pass wall time and gate count changes
- Whether MinimumPoint triggers backtracking
- Which passes actually contribute reductions

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
    ops = dag.count_ops()
    # Count 2Q gates by checking op_nodes
    n2q = 0
    for node in dag.op_nodes():
        if len(node.qargs) == 2:
            n2q += 1
    return {"size": size, "depth": depth, "2q_gates": n2q}


def profile_optimization_loop(circuit, backend, optimization_level=3):
    """Run transpilation and profile the optimization stage loop.

    Returns dict with per-iteration, per-pass timing and gate count data.
    """
    target = backend.target
    basis_gates = list(target.operation_names)
    coupling_map = target.build_coupling_map()

    # Step 1: Run init + layout + routing + translation (everything before optimization)
    pm = generate_preset_pass_manager(optimization_level, backend=backend)

    # We'll use the full pass manager but intercept the optimization stage.
    # First, run everything except optimization by setting optimization to None temporarily.
    pm_pre_opt = generate_preset_pass_manager(optimization_level, backend=backend)
    pm_pre_opt.optimization = PassManager()  # Empty — skip optimization

    t0 = time.perf_counter()
    pre_opt_circuit = pm_pre_opt.run(circuit)
    pre_opt_time = time.perf_counter() - t0

    # Convert to DAG for pass-by-pass execution
    from qiskit.converters import circuit_to_dag, dag_to_circuit
    dag = circuit_to_dag(pre_opt_circuit)

    pre_opt_stats = get_circuit_stats(dag)

    # Step 2: Build the level 3 optimization passes manually
    passes_in_loop = [
        ("ConsolidateBlocks", ConsolidateBlocks(
            basis_gates=basis_gates, target=target,
        )),
        ("UnitarySynthesis", UnitarySynthesis(
            basis_gates, target=target,
        )),
        ("RemoveIdentityEquivalent", RemoveIdentityEquivalent(target=target)),
        ("Optimize1qGatesDecomposition", Optimize1qGatesDecomposition(
            basis=basis_gates, target=target,
        )),
        ("CommutativeCancellation", CommutativeCancellation(target=target)),
        ("ContractIdleWiresInControlFlow", ContractIdleWiresInControlFlow()),
    ]

    # Conditional unroll (BasisTranslator)
    gates_in_basis = GatesInBasis(basis_gates, target=target)

    # MinimumPoint tracking
    size_pass = Size(recurse=True)
    depth_pass = Depth(recurse=True)
    min_point = MinimumPoint(["depth", "size"], "optimization_loop", backtrack_depth=5)

    # Step 3: Run the loop manually with instrumentation
    results = {
        "pre_opt_time": pre_opt_time,
        "pre_opt_stats": pre_opt_stats,
        "iterations": [],
    }

    property_set = {}
    # Initialize property set entries that passes expect
    from qiskit.passmanager import PropertySet
    prop_set = PropertySet()

    max_iterations = 20  # Safety cap (real cap is 1000)

    for iteration in range(max_iterations):
        iter_data = {"iteration": iteration + 1, "passes": []}

        for pass_name, pass_inst in passes_in_loop:
            # Set up property set for the pass
            pass_inst.property_set = prop_set

            stats_before = get_circuit_stats(dag)
            t_start = time.perf_counter()

            try:
                new_dag = pass_inst.run(dag)
                if new_dag is not None:
                    dag = new_dag
            except Exception as e:
                iter_data["passes"].append({
                    "name": pass_name,
                    "time_ms": 0,
                    "error": str(e),
                })
                continue

            t_elapsed = time.perf_counter() - t_start
            stats_after = get_circuit_stats(dag)

            iter_data["passes"].append({
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
            })

        # Run GatesInBasis check
        gates_in_basis.property_set = prop_set
        gates_in_basis.run(dag)
        needs_unroll = not prop_set.get("all_gates_in_basis", True)
        iter_data["needs_unroll"] = needs_unroll

        if needs_unroll:
            # Run translation
            translation_pm = generate_preset_pass_manager(
                optimization_level, backend=backend
            )
            # Get just the translation stage
            trans_dag = circuit_to_dag(translation_pm.translation.run(dag_to_circuit(dag)))
            if trans_dag is not None:
                dag = trans_dag
            iter_data["ran_translation"] = True
        else:
            iter_data["ran_translation"] = False

        # Run Size/Depth/MinimumPoint
        size_pass.property_set = prop_set
        depth_pass.property_set = prop_set
        min_point.property_set = prop_set

        size_pass.run(dag)
        depth_pass.run(dag)
        new_dag = min_point.run(dag)
        if new_dag is not None:
            dag = new_dag

        iter_stats = get_circuit_stats(dag)
        iter_data["end_stats"] = iter_stats
        iter_data["minimum_point_reached"] = prop_set.get("optimization_loop_minimum_point", False)

        results["iterations"].append(iter_data)

        # Check convergence
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
    print(f"\n{'=' * 80}")
    print(f"Circuit: {name}")
    print(f"{'=' * 80}")
    print(f"Pre-optimization: {results['pre_opt_time']*1000:.0f}ms "
          f"(size={results['pre_opt_stats']['size']}, "
          f"2q={results['pre_opt_stats']['2q_gates']}, "
          f"depth={results['pre_opt_stats']['depth']})")
    print(f"Converged at iteration {results['converged_at']} ({results['convergence_reason']})")
    print(f"Final: size={results['final_stats']['size']}, "
          f"2q={results['final_stats']['2q_gates']}, "
          f"depth={results['final_stats']['depth']}")

    # Per-iteration summary
    print(f"\n{'Iter':>4s}  {'Size':>6s}  {'2Q':>6s}  {'Depth':>6s}  {'Time(ms)':>9s}  {'Unroll?':>7s}  {'MinPt?':>6s}")
    print("-" * 55)
    for it in results["iterations"]:
        total_time = sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
        s = it["end_stats"]
        print(f"{it['iteration']:4d}  {s['size']:6d}  {s['2q_gates']:6d}  {s['depth']:6d}  "
              f"{total_time:9.1f}  {'YES' if it.get('ran_translation') else 'no':>7s}  "
              f"{'YES' if it.get('minimum_point_reached') else 'no':>6s}")

    # Per-pass cumulative time and total gate delta
    print(f"\nPer-pass cumulative stats:")
    print(f"{'Pass':>35s}  {'Total ms':>9s}  {'Avg ms':>8s}  {'Total size delta':>16s}  {'Total 2Q delta':>15s}")
    print("-" * 90)
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
        print(f"{pname:>35s}  {total_t:9.1f}  {avg_t:8.1f}  {total_sd:16d}  {total_2qd:15d}")


def print_summary(all_results):
    """Print comparison summary across all circuits."""
    print(f"\n{'=' * 80}")
    print("SUMMARY")
    print(f"{'=' * 80}")

    # Table 1: Loop Convergence & Timing
    print(f"\n--- Table 1: Loop Convergence & Timing ---\n")
    print(f"{'Circuit':>20s}  {'Iters':>5s}  {'Productive':>10s}  {'Why Stopped':>20s}  "
          f"{'Opt(ms)':>8s}  {'Pre-Opt(ms)':>11s}  {'Opt %':>6s}")
    print("-" * 90)
    for name, results in all_results.items():
        n_iters = results["converged_at"]
        opt_time = sum(
            sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
            for it in results["iterations"]
        )
        pre_time = results["pre_opt_time"] * 1000
        opt_pct = (opt_time / (opt_time + pre_time) * 100) if (opt_time + pre_time) > 0 else 0

        # Determine productive iterations: how many iterations actually changed 2Q gate count
        productive = 0
        prev_2q = results["pre_opt_stats"]["2q_gates"]
        for it in results["iterations"]:
            cur_2q = it["end_stats"]["2q_gates"]
            if cur_2q != prev_2q:
                productive += 1
            prev_2q = cur_2q

        # Determine why it stopped
        # Check if last iterations had same score (fixed point) or oscillated (backtrack)
        if n_iters <= 3:
            reason = "fixed point (iter 1-2)"
        else:
            # Check if iteration 1 result == final result
            iter1_stats = results["iterations"][0]["end_stats"]
            final_stats = results["final_stats"]
            if (iter1_stats["2q_gates"] == final_stats["2q_gates"] and
                    iter1_stats["depth"] == final_stats["depth"]):
                reason = f"backtrack (oscillation)"
            else:
                iter2_stats = results["iterations"][1]["end_stats"]
                if (iter2_stats["2q_gates"] == final_stats["2q_gates"]):
                    reason = f"backtrack (from iter 2)"
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

    # Table 3: Per-Pass Effectiveness (time and 2Q gate reduction)
    print(f"\n--- Table 3: Per-Pass Effectiveness (cumulative across all iterations) ---\n")
    pass_names = ["ConsolidateBlocks", "UnitarySynthesis", "RemoveIdentityEquivalent",
                  "Optimize1qGatesDecomposition", "CommutativeCancellation",
                  "ContractIdleWiresInControlFlow"]
    # Header
    header = f"{'Pass':>30s}"
    for name in all_results:
        short = name[:10]
        header += f"  {short:>12s}"
    print(header)
    print("-" * (30 + 14 * len(all_results)))

    # Time per pass
    print("\n  Time (ms):")
    for pname in pass_names:
        row = f"  {pname:>28s}"
        for name, results in all_results.items():
            total_t = 0
            for it in results["iterations"]:
                for p in it["passes"]:
                    if p["name"] == pname and "time_ms" in p:
                        total_t += p["time_ms"]
            row += f"  {total_t:12.0f}"
        print(row)

    # 2Q delta per pass
    print("\n  2Q Gate Delta:")
    for pname in pass_names:
        row = f"  {pname:>28s}"
        for name, results in all_results.items():
            total_2q = 0
            for it in results["iterations"]:
                for p in it["passes"]:
                    if p["name"] == pname:
                        total_2q += p.get("2q_delta", 0)
            row += f"  {total_2q:12d}"
        print(row)

    # Size delta per pass
    print("\n  Total Gate Delta:")
    for pname in pass_names:
        row = f"  {pname:>28s}"
        for name, results in all_results.items():
            total_s = 0
            for it in results["iterations"]:
                for p in it["passes"]:
                    if p["name"] == pname:
                        total_s += p.get("size_delta", 0)
            row += f"  {total_s:12d}"
        print(row)

    # Table 4: Time breakdown (% of optimization time per pass)
    print(f"\n--- Table 4: Time Breakdown (% of optimization stage) ---\n")
    header = f"{'Pass':>30s}"
    for name in all_results:
        short = name[:10]
        header += f"  {short:>10s}"
    print(header)
    print("-" * (30 + 12 * len(all_results)))
    for pname in pass_names:
        row = f"  {pname:>28s}"
        for name, results in all_results.items():
            total_opt = sum(
                sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
                for it in results["iterations"]
            )
            pass_t = 0
            for it in results["iterations"]:
                for p in it["passes"]:
                    if p["name"] == pname and "time_ms" in p:
                        pass_t += p["time_ms"]
            pct = (pass_t / total_opt * 100) if total_opt > 0 else 0
            row += f"  {pct:9.1f}%"
        print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Optimization level: 3")
    print(f"Basis gates: {list(backend.target.operation_names)[:5]}...")

    all_results = {}

    for name, builder in CIRCUITS.items():
        print(f"\n--- Building {name}...")
        t0 = time.perf_counter()
        circuit = builder()
        build_time = time.perf_counter() - t0
        print(f"    Built in {build_time*1000:.0f}ms: {circuit.num_qubits}Q, "
              f"{circuit.size()} gates")

        print(f"    Profiling optimization loop...")
        try:
            results = profile_optimization_loop(circuit, backend, optimization_level=3)
            all_results[name] = results
            print_results(name, results)
        except Exception as e:
            print(f"    ERROR: {e}")
            import traceback
            traceback.print_exc()

    print_summary(all_results)


if __name__ == "__main__":
    main()
