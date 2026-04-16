"""Profile optimization loop on a real chemistry circuit: [4Fe-4S] LUCJ.

72-qubit Local Unitary Cluster Jastrow ansatz — the kind of circuit
real users run at optimization_level=3 on IBM hardware.

Tests loop vs no-loop at both Level 2 and Level 3, plus pass ordering.

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/test_fe4s4.py
"""

import json
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

import ffsim
from qiskit import QuantumCircuit, QuantumRegister
from qiskit.transpiler import PassManager
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
# Build the fe4s4 LUCJ circuit
# ---------------------------------------------------------------------------

def build_fe4s4_circuit():
    """Build the 72-qubit [4Fe-4S] LUCJ circuit from pre-computed parameters."""
    params_path = "/Users/hfwen/IBMWORK/QCSC/sbd/data/fe4s4/parameters_fe4s4.json"

    with open(params_path) as f:
        params_data = json.load(f)

    norb = params_data["norb"]
    nelec = tuple(params_data["nelec"])
    n_reps = len(params_data["params"])
    param_vec = np.array(params_data["params"][0])
    alpha_alpha_indices = [tuple(p) for p in params_data["alpha_alpha_indices"]]
    alpha_beta_indices = [tuple(p) for p in params_data["alpha_beta_indices"]]

    ucj_op = ffsim.UCJOpSpinBalanced.from_parameters(
        param_vec,
        norb=norb,
        n_reps=n_reps,
        interaction_pairs=(alpha_alpha_indices, alpha_beta_indices),
        with_final_orbital_rotation=True,
    )

    num_qubits = 2 * norb
    qubits = QuantumRegister(num_qubits, name="q")
    circuit = QuantumCircuit(qubits)
    circuit.append(ffsim.qiskit.PrepareHartreeFockJW(norb, nelec), qubits)
    circuit.append(ffsim.qiskit.UCJOpSpinBalancedJW(ucj_op), qubits)
    circuit.measure_all()

    print(f"  [4Fe-4S] LUCJ: {num_qubits}Q, {norb} orbitals, "
          f"({nelec[0]}a,{nelec[1]}b) electrons, {n_reps} reps")

    return circuit


# ---------------------------------------------------------------------------
# Reuse instrumentation from main script
# ---------------------------------------------------------------------------

def get_stats(dag):
    size = dag.size()
    depth = dag.depth()
    n2q = sum(1 for node in dag.op_nodes() if len(node.qargs) == 2)
    return {"size": size, "depth": depth, "2q": n2q}


def run_pass_instrumented(dag, pass_name, pass_inst, prop_set):
    pass_inst.property_set = prop_set
    stats_before = get_stats(dag)
    t_start = time.perf_counter()
    try:
        new_dag = pass_inst.run(dag)
        if new_dag is not None:
            dag = new_dag
    except Exception as e:
        return dag, {"name": pass_name, "time_ms": 0, "error": str(e)}
    t_elapsed = time.perf_counter() - t_start
    stats_after = get_stats(dag)
    return dag, {
        "name": pass_name,
        "time_ms": t_elapsed * 1000,
        "2q_before": stats_before["2q"],
        "2q_after": stats_after["2q"],
        "2q_delta": stats_after["2q"] - stats_before["2q"],
        "size_delta": stats_after["size"] - stats_before["size"],
    }


def build_passes(backend):
    target = backend.target
    basis_gates = list(target.operation_names)
    return (
        ("ConsolidateBlocks", ConsolidateBlocks(basis_gates=basis_gates, target=target)),
        ("UnitarySynthesis", UnitarySynthesis(basis_gates, target=target)),
        ("RemoveIdentityEquivalent", RemoveIdentityEquivalent(target=target)),
        ("Optimize1qGatesDecomposition", Optimize1qGatesDecomposition(basis=basis_gates, target=target)),
        ("CommutativeCancellation", CommutativeCancellation(target=target)),
    )


# ---------------------------------------------------------------------------
# Profile the optimization loop
# ---------------------------------------------------------------------------

def profile_loop(pre_opt_circuit, backend, level, max_iters=20):
    """Run optimization loop with instrumentation."""
    target = backend.target
    basis_gates = list(target.operation_names)

    dag = circuit_to_dag(pre_opt_circuit)
    pre_stats = get_stats(dag)

    prop_set = PropertySet()

    CB, US, RI, O1, CC = build_passes(backend)

    if level == 2:
        # Pre-loop: ConsolidateBlocks + UnitarySynthesis
        pre_loop_data = []
        for name, p in [CB, US]:
            dag, pdata = run_pass_instrumented(dag, name, p, prop_set)
            pre_loop_data.append(pdata)

        loop_passes = [
            ("RemoveIdentityEquivalent", RemoveIdentityEquivalent(target=target)),
            ("Optimize1qGatesDecomposition", Optimize1qGatesDecomposition(basis=basis_gates, target=target)),
            ("CommutativeCancellation", CommutativeCancellation(target=target)),
            ("ContractIdleWiresInControlFlow", ContractIdleWiresInControlFlow()),
        ]

        size_pass = Size(recurse=True)
        depth_pass = Depth(recurse=True)
        fp_size = FixedPoint("size")
        fp_depth = FixedPoint("depth")

        iterations = []
        for i in range(max_iters):
            iter_passes = []
            for pname, pinst in loop_passes:
                dag, pdata = run_pass_instrumented(dag, pname, pinst, prop_set)
                iter_passes.append(pdata)

            for p in [size_pass, depth_pass, fp_size, fp_depth]:
                p.property_set = prop_set
                p.run(dag)

            converged = (prop_set.get("size_fixed_point", False) and
                         prop_set.get("depth_fixed_point", False))

            stats = get_stats(dag)
            iterations.append({"passes": iter_passes, "stats": stats, "converged": converged})

            if converged:
                break

        return {
            "pre_stats": pre_stats,
            "pre_loop": pre_loop_data,
            "iterations": iterations,
            "final_stats": get_stats(dag),
            "n_iters": len(iterations),
        }

    else:  # level 3
        loop_passes_template = [
            ("ConsolidateBlocks", lambda: ConsolidateBlocks(basis_gates=basis_gates, target=target)),
            ("UnitarySynthesis", lambda: UnitarySynthesis(basis_gates, target=target)),
            ("RemoveIdentityEquivalent", lambda: RemoveIdentityEquivalent(target=target)),
            ("Optimize1qGatesDecomposition", lambda: Optimize1qGatesDecomposition(basis=basis_gates, target=target)),
            ("CommutativeCancellation", lambda: CommutativeCancellation(target=target)),
            ("ContractIdleWiresInControlFlow", lambda: ContractIdleWiresInControlFlow()),
        ]

        size_pass = Size(recurse=True)
        depth_pass = Depth(recurse=True)
        min_point = MinimumPoint(["depth", "size"], "opt_loop", backtrack_depth=5)

        iterations = []
        for i in range(max_iters):
            iter_passes = []
            for pname, pfactory in loop_passes_template:
                pinst = pfactory()
                dag, pdata = run_pass_instrumented(dag, pname, pinst, prop_set)
                iter_passes.append(pdata)

            for p in [size_pass, depth_pass, min_point]:
                p.property_set = prop_set
                new_dag = p.run(dag)
                if new_dag is not None:
                    dag = new_dag

            converged = prop_set.get("opt_loop_minimum_point", False)

            stats = get_stats(dag)
            iterations.append({"passes": iter_passes, "stats": stats, "converged": converged})

            if converged:
                break

        return {
            "pre_stats": pre_stats,
            "pre_loop": [],
            "iterations": iterations,
            "final_stats": get_stats(dag),
            "n_iters": len(iterations),
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")

    print(f"\nBuilding fe4s4 LUCJ circuit...")
    circuit = build_fe4s4_circuit()

    # Note: ffsim uses a pre_init stage for circuit decomposition
    # We'll include it in the pre-optimization pipeline
    print(f"\nPre-optimizing (init + layout + routing + translation)...")

    results = {}

    for level in [2, 3]:
        print(f"\n{'='*60}")
        print(f"Level {level}")
        print(f"{'='*60}")

        # Pre-optimize
        pm = generate_preset_pass_manager(level, backend=backend)
        pm.optimization = PassManager()  # Skip optimization
        pm.pre_init = ffsim.qiskit.PRE_INIT  # ffsim decomposition

        t0 = time.perf_counter()
        pre_opt = pm.run(circuit)
        pre_opt_time = time.perf_counter() - t0
        pre_dag = circuit_to_dag(pre_opt)
        pre_stats = get_stats(pre_dag)
        print(f"  Pre-opt: {pre_opt_time*1000:.0f}ms — "
              f"size={pre_stats['size']}, 2q={pre_stats['2q']}, depth={pre_stats['depth']}")

        # Full loop
        print(f"\n  Full loop...")
        t0 = time.perf_counter()
        r_loop = profile_loop(pre_opt, backend, level)
        loop_time = time.perf_counter() - t0

        print(f"    Iterations: {r_loop['n_iters']}")
        print(f"    Time: {loop_time*1000:.0f}ms")
        print(f"    Final: 2q={r_loop['final_stats']['2q']}, "
              f"size={r_loop['final_stats']['size']}, "
              f"depth={r_loop['final_stats']['depth']}")

        if r_loop["pre_loop"]:
            for p in r_loop["pre_loop"]:
                if "error" not in p:
                    print(f"    Pre-loop {p['name']}: {p['time_ms']:.0f}ms, "
                          f"2q: {p['2q_before']}→{p['2q_after']} ({p['2q_delta']:+d})")

        for i, it in enumerate(r_loop["iterations"]):
            iter_time = sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
            print(f"    Iter {i+1}: 2q={it['stats']['2q']}, "
                  f"size={it['stats']['size']}, depth={it['stats']['depth']}, "
                  f"time={iter_time:.0f}ms"
                  f"{' CONVERGED' if it['converged'] else ''}")

            for p in it["passes"]:
                if "error" in p:
                    print(f"      {p['name']}: ERROR {p['error']}")
                elif p.get("2q_delta", 0) != 0 or p.get("size_delta", 0) != 0:
                    print(f"      {p['name']}: {p['time_ms']:.0f}ms, "
                          f"2q: {p['2q_delta']:+d}, size: {p['size_delta']:+d}")

        # No-loop (single iteration)
        print(f"\n  No loop (1 iteration)...")
        t0 = time.perf_counter()
        r_noloop = profile_loop(pre_opt, backend, level, max_iters=1)
        noloop_time = time.perf_counter() - t0

        print(f"    Time: {noloop_time*1000:.0f}ms")
        print(f"    Final: 2q={r_noloop['final_stats']['2q']}, "
              f"size={r_noloop['final_stats']['size']}, "
              f"depth={r_noloop['final_stats']['depth']}")

        # Compare
        loop_2q = r_loop["final_stats"]["2q"]
        noloop_2q = r_noloop["final_stats"]["2q"]
        diff = noloop_2q - loop_2q
        print(f"\n  Loop vs No-Loop:")
        print(f"    2Q gates:  loop={loop_2q}, no-loop={noloop_2q}, diff={diff:+d}")
        print(f"    Opt time:  loop={loop_time*1000:.0f}ms, no-loop={noloop_time*1000:.0f}ms, "
              f"speedup={loop_time/noloop_time:.1f}x")

        results[level] = {
            "pre_opt_time": pre_opt_time,
            "pre_stats": pre_stats,
            "loop": r_loop,
            "loop_time": loop_time,
            "noloop": r_noloop,
            "noloop_time": noloop_time,
        }

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY: [4Fe-4S] LUCJ (72Q)")
    print(f"{'='*60}")
    print(f"\n{'':>15s}  {'L2 Loop':>10s}  {'L2 NoLoop':>10s}  {'L3 Loop':>10s}  {'L3 NoLoop':>10s}")
    print("-" * 60)

    for metric, key in [("2Q gates", "2q"), ("Total gates", "size"), ("Depth", "depth")]:
        row = f"{metric:>15s}"
        for level in [2, 3]:
            for mode in ["loop", "noloop"]:
                val = results[level][mode]["final_stats"][key]
                row += f"  {val:10d}"
        print(row)

    row = f"{'Opt time (ms)':>15s}"
    for level in [2, 3]:
        for mode in ["loop_time", "noloop_time"]:
            val = results[level][mode] * 1000
            row += f"  {val:10.0f}"
    print(row)

    row = f"{'Iterations':>15s}"
    for level in [2, 3]:
        for mode in ["loop", "noloop"]:
            val = results[level][mode]["n_iters"]
            row += f"  {val:10d}"
    print(row)


if __name__ == "__main__":
    main()
