"""Test optimization loop on additional circuit families.

Extends our investigation to circuits we haven't tested yet:
  - Grover's search (Toffoli-heavy)
  - Ripple-carry adder (structured arithmetic)
  - Random circuit (worst-case stress test)
  - GHZ state (simple, widely used)
  - Phase estimation (QPE)
  - Toffoli cascade (chain of CCX gates)

Tests loop vs no-loop at Level 3 on FakeTorino.

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/test_more_circuits.py
"""

import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import CDKMRippleCarryAdder
from qiskit.circuit.random import random_circuit
from qiskit.transpiler import PassManager
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.transpiler.passes import (
    ConsolidateBlocks,
    UnitarySynthesis,
    RemoveIdentityEquivalent,
    Optimize1qGatesDecomposition,
    CommutativeCancellation,
    ContractIdleWiresInControlFlow,
    Size,
    Depth,
)
from qiskit.transpiler.passes.utils.minimum_point import MinimumPoint
from qiskit.transpiler.passes.utils.fixed_point import FixedPoint
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit.passmanager import PropertySet
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Circuit builders
# ---------------------------------------------------------------------------

def build_grover_50():
    """~50-qubit Grover's search — Toffoli-heavy with small oracles.

    Uses many small MCX gates (3-5 controls) instead of one giant MCX,
    which is more realistic and practical to transpile.
    """
    n = 50
    qc = QuantumCircuit(n)
    # Initial superposition
    qc.h(range(n))
    # 2 Grover iterations with a composite oracle
    for _ in range(2):
        # Oracle: multiple small multi-controlled gates (realistic)
        # Each marks a different subset of qubits
        for start in range(0, n - 4, 5):
            controls = list(range(start, start + 4))
            target = start + 4
            qc.mcx(controls, target)  # 4-control Toffoli
        for start in range(0, n - 3, 4):
            controls = list(range(start, start + 2))
            target = min(start + 3, n - 1)
            qc.ccx(controls[0], controls[1], target)  # Regular Toffoli
        # Diffuser with small MCX
        qc.h(range(n))
        qc.x(range(n))
        for start in range(0, n - 4, 5):
            controls = list(range(start, start + 4))
            target = start + 4
            qc.mcx(controls, target)
        qc.x(range(n))
        qc.h(range(n))
    qc.measure_all()
    return qc


def build_adder_80():
    """~80-qubit ripple-carry adder — structured arithmetic."""
    # CDKMRippleCarryAdder(num_state_qubits) uses 2*n + 2 qubits
    # n=39 gives 80 qubits
    adder = CDKMRippleCarryAdder(39, kind="half")
    qc = QuantumCircuit(adder.num_qubits)
    # Set up some input state
    for i in range(0, adder.num_qubits, 3):
        qc.x(i)
    qc.append(adder, range(adder.num_qubits))
    qc.measure_all()
    return qc


def build_random_80():
    """80-qubit random circuit depth 40 — worst-case stress test."""
    qc = random_circuit(80, 40, max_operands=2, seed=42)
    qc.measure_all()
    return qc


def build_ghz_100():
    """100-qubit GHZ state — simple, widely used."""
    n = 100
    qc = QuantumCircuit(n)
    qc.h(0)
    for i in range(n - 1):
        qc.cx(i, i + 1)
    qc.measure_all()
    return qc


def build_qpe_50():
    """~50-qubit Quantum Phase Estimation (manual construction).

    49 counting qubits + 1 state qubit. Controlled-P gates + inverse QFT.
    Built manually because PhaseEstimation library class is too slow at 49Q.
    """
    n_counting = 49
    n = n_counting + 1
    qc = QuantumCircuit(n)

    # Prepare eigenstate |1>
    qc.x(n_counting)

    # Hadamard on counting qubits
    qc.h(range(n_counting))

    # Controlled-P gates: qubit k applies P(2^k * theta) on state qubit
    theta = np.pi / 3
    for k in range(n_counting):
        qc.cp((2 ** k) * theta, k, n_counting)

    # Inverse QFT on counting register
    for i in range(n_counting // 2):
        qc.swap(i, n_counting - 1 - i)
    for i in range(n_counting):
        for j in range(i):
            qc.cp(-np.pi / (2 ** (i - j)), j, i)
        qc.h(i)

    qc.measure_all()
    return qc


def build_toffoli_cascade_90():
    """90-qubit Toffoli cascade — chain of CCX gates."""
    n = 90
    qc = QuantumCircuit(n)
    # Initial state
    qc.x(0)
    qc.x(1)
    # Chain of Toffoli gates: CCX(i, i+1, i+2) for i=0,2,4,...
    for i in range(0, n - 2, 2):
        qc.ccx(i, i + 1, i + 2)
    # Second layer offset
    for i in range(1, n - 2, 2):
        qc.ccx(i, i + 1, i + 2)
    # Third layer
    for i in range(0, n - 2, 3):
        qc.ccx(i, i + 1, min(i + 2, n - 1))
    qc.measure_all()
    return qc


CIRCUITS = {
    "Grover_50": build_grover_50,
    "Adder_80": build_adder_80,
    "Random_80": build_random_80,
    "GHZ_100": build_ghz_100,
    "QPE_50": build_qpe_50,
    "Toffoli_90": build_toffoli_cascade_90,
}


# ---------------------------------------------------------------------------
# Profiling (simplified from main script — loop vs no-loop only)
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
        return dag, {"name": pass_name, "time_ms": 0, "error": str(e),
                     "2q_delta": 0, "size_delta": 0}
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


def profile_level(pre_opt_circuit, backend, level, max_iters=20):
    """Profile optimization at given level. Returns results dict."""
    target = backend.target
    basis_gates = list(target.operation_names)

    dag = circuit_to_dag(pre_opt_circuit)
    pre_stats = get_stats(dag)
    prop_set = PropertySet()

    pre_loop_data = []

    if level == 2:
        # Pre-loop passes
        for pname, pinst in [
            ("ConsolidateBlocks", ConsolidateBlocks(basis_gates=basis_gates, target=target)),
            ("UnitarySynthesis", UnitarySynthesis(basis_gates, target=target)),
        ]:
            dag, pdata = run_pass_instrumented(dag, pname, pinst, prop_set)
            pre_loop_data.append(pdata)

        loop_passes_factories = [
            ("RemoveIdentityEquivalent", lambda: RemoveIdentityEquivalent(target=target)),
            ("Optimize1qGatesDecomposition", lambda: Optimize1qGatesDecomposition(basis=basis_gates, target=target)),
            ("CommutativeCancellation", lambda: CommutativeCancellation(target=target)),
            ("ContractIdleWiresInControlFlow", lambda: ContractIdleWiresInControlFlow()),
        ]

        size_pass = Size(recurse=True)
        depth_pass = Depth(recurse=True)
        fp_size = FixedPoint("size")
        fp_depth = FixedPoint("depth")

        iterations = []
        for i in range(max_iters):
            iter_passes = []
            for pname, pfactory in loop_passes_factories:
                dag, pdata = run_pass_instrumented(dag, pname, pfactory(), prop_set)
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

    else:  # level 3
        loop_passes_factories = [
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
            for pname, pfactory in loop_passes_factories:
                dag, pdata = run_pass_instrumented(dag, pname, pfactory(), prop_set)
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
        "pre_loop": pre_loop_data,
        "iterations": iterations,
        "final_stats": get_stats(dag),
        "n_iters": len(iterations),
    }


def get_opt_time(r):
    """Total optimization time from results."""
    t = sum(p["time_ms"] for p in r["pre_loop"] if "time_ms" in p)
    for it in r["iterations"]:
        t += sum(p["time_ms"] for p in it["passes"] if "time_ms" in p)
    return t


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Testing loop vs no-loop on additional circuit families\n")

    all_data = {}

    for cname, builder in CIRCUITS.items():
        print(f"\n{'='*70}")
        print(f"Building {cname}...")
        try:
            t0 = time.perf_counter()
            circuit = builder()
            build_time = time.perf_counter() - t0
            print(f"  Built in {build_time*1000:.0f}ms: {circuit.num_qubits}Q, "
                  f"{circuit.size()} gates")
        except Exception as e:
            print(f"  BUILD ERROR: {e}")
            import traceback
            traceback.print_exc()
            continue

        circuit_data = {}

        for level in [2, 3]:
            print(f"\n  Level {level}:")

            # Pre-optimize
            try:
                pm = generate_preset_pass_manager(level, backend=backend)
                pm.optimization = PassManager()
                t0 = time.perf_counter()
                pre_opt = pm.run(circuit)
                pre_time = time.perf_counter() - t0
                pre_dag = circuit_to_dag(pre_opt)
                pre_stats = get_stats(pre_dag)
                print(f"    Pre-opt: {pre_time*1000:.0f}ms — "
                      f"size={pre_stats['size']}, 2q={pre_stats['2q']}, depth={pre_stats['depth']}")
            except Exception as e:
                print(f"    PRE-OPT ERROR: {e}")
                import traceback
                traceback.print_exc()
                continue

            # Full loop
            try:
                t0 = time.perf_counter()
                r_loop = profile_level(pre_opt, backend, level)
                loop_time = time.perf_counter() - t0
                print(f"    Loop:    {r_loop['n_iters']} iters, {get_opt_time(r_loop):.0f}ms opt, "
                      f"2q={r_loop['final_stats']['2q']}, "
                      f"size={r_loop['final_stats']['size']}, "
                      f"depth={r_loop['final_stats']['depth']}")

                # Show per-iteration detail
                if r_loop["pre_loop"]:
                    for p in r_loop["pre_loop"]:
                        if "error" not in p and p.get("2q_delta", 0) != 0:
                            print(f"      Pre-loop {p['name']}: 2q {p['2q_delta']:+d}")
                for i, it in enumerate(r_loop["iterations"]):
                    active = [p for p in it["passes"]
                              if "error" not in p and (p.get("2q_delta", 0) != 0)]
                    if active:
                        detail = ", ".join(f"{p['name']}: 2q {p['2q_delta']:+d}" for p in active)
                        print(f"      Iter {i+1}: {detail}")

            except Exception as e:
                print(f"    LOOP ERROR: {e}")
                import traceback
                traceback.print_exc()
                continue

            # No-loop
            try:
                t0 = time.perf_counter()
                r_noloop = profile_level(pre_opt, backend, level, max_iters=1)
                noloop_time = time.perf_counter() - t0
                print(f"    NoLoop:  {get_opt_time(r_noloop):.0f}ms opt, "
                      f"2q={r_noloop['final_stats']['2q']}, "
                      f"size={r_noloop['final_stats']['size']}, "
                      f"depth={r_noloop['final_stats']['depth']}")
            except Exception as e:
                print(f"    NOLOOP ERROR: {e}")
                import traceback
                traceback.print_exc()
                continue

            # Compare
            loop_2q = r_loop["final_stats"]["2q"]
            noloop_2q = r_noloop["final_stats"]["2q"]
            diff = noloop_2q - loop_2q
            t_loop = get_opt_time(r_loop)
            t_noloop = get_opt_time(r_noloop)
            speedup = t_loop / t_noloop if t_noloop > 0 else 0
            regressed = "YES" if diff > 0 else "no"
            print(f"    Diff:    2q={diff:+d} ({regressed}), "
                  f"speedup={speedup:.1f}x")

            circuit_data[level] = {
                "pre_time": pre_time,
                "pre_stats": pre_stats,
                "loop": r_loop,
                "noloop": r_noloop,
            }

        all_data[cname] = circuit_data

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY: Loop vs No-Loop on Additional Circuits")
    print(f"{'='*70}")

    for level in [2, 3]:
        print(f"\n--- Level {level} ---\n")
        print(f"{'Circuit':>20s}  {'Pre-Opt 2Q':>10s}  {'Loop 2Q':>8s}  {'NoLoop 2Q':>10s}  "
              f"{'Diff':>6s}  {'Regress?':>8s}  {'Loop(ms)':>9s}  {'NoLoop(ms)':>10s}  {'Speedup':>8s}")
        print("-" * 105)

        for cname in CIRCUITS:
            if cname not in all_data or level not in all_data[cname]:
                print(f"{cname:>20s}  {'SKIPPED':>10s}")
                continue
            d = all_data[cname][level]
            pre_2q = d["pre_stats"]["2q"]
            loop_2q = d["loop"]["final_stats"]["2q"]
            noloop_2q = d["noloop"]["final_stats"]["2q"]
            diff = noloop_2q - loop_2q
            regressed = "YES" if diff > 0 else "no"
            t_loop = get_opt_time(d["loop"])
            t_noloop = get_opt_time(d["noloop"])
            speedup = t_loop / t_noloop if t_noloop > 0 else 0
            print(f"{cname:>20s}  {pre_2q:10d}  {loop_2q:8d}  {noloop_2q:10d}  "
                  f"{diff:+6d}  {regressed:>8s}  {t_loop:9.0f}  {t_noloop:10.0f}  {speedup:7.1f}x")

    # Combined summary with previous circuits
    print(f"\n{'='*70}")
    print("ALL CIRCUITS TESTED (this run + previous)")
    print(f"{'='*70}")
    print(f"\nPrevious (from profile_optimization_loop.py):")
    print(f"  QFT_100, QV_100, EfficientSU2_100, QAOA_100, BV_100, Heisenberg_100")
    print(f"  + fe4s4 LUCJ (72Q)")
    print(f"  Results: 0/7 regressed at L2, 1/7 regressed at L3 (QFT, +8 gates = 0.09%)")
    print(f"\nThis run:")
    for level in [2, 3]:
        n_total = 0
        n_regressed = 0
        max_regression = 0
        for cname in CIRCUITS:
            if cname in all_data and level in all_data[cname]:
                d = all_data[cname][level]
                loop_2q = d["loop"]["final_stats"]["2q"]
                noloop_2q = d["noloop"]["final_stats"]["2q"]
                diff = noloop_2q - loop_2q
                n_total += 1
                if diff > 0:
                    n_regressed += 1
                    max_regression = max(max_regression, diff)
        print(f"  Level {level}: {n_regressed}/{n_total} regressed"
              f"{f', max regression: {max_regression} gates' if n_regressed > 0 else ''}")


if __name__ == "__main__":
    main()
