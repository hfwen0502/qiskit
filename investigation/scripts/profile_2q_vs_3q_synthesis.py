"""A/B comparison: 2Q-only vs 3Q block synthesis.

Tests the hypothesis that switching from Collect2qBlocks to
CollectMultiQBlocks(max_block_size=3) would regress gate counts because
ConsolidateBlocks unconditionally packages >2Q blocks as UnitaryGate,
and UnitarySynthesis resynthesizes them with QSD (~20 CX), which is worse
than the original 6-9 CX gates in the typical 3Q block.

Both variants start from the same pre-optimized circuit (init + layout +
routing + translation at Level 2, no optimization). The optimization stage
is then run separately for each variant.

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_2q_vs_3q_synthesis.py
"""

import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import (
    CDKMRippleCarryAdder,
    EfficientSU2,
    QFT,
    QuantumVolume,
)
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
    Collect1qRuns,
)
from qiskit.transpiler.passes.optimization.collect_2q_blocks import Collect2qBlocks
from qiskit.transpiler.passes.optimization.collect_multiqubit_blocks import (
    CollectMultiQBlocks,
)
from qiskit.transpiler.passes.utils.fixed_point import FixedPoint
from qiskit.converters import circuit_to_dag
from qiskit.passmanager import PropertySet
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Circuit builders (same as profile_3q_blocks.py)
# ---------------------------------------------------------------------------

def build_qft_100():
    return QFT(100, name="QFT_100")

def build_qv_100():
    return QuantumVolume(100, depth=10, seed=42)

def build_efsu2_100():
    qc = EfficientSU2(100, reps=1, entanglement="linear")
    params = {p: np.random.default_rng(42).uniform(0, 2 * np.pi) for p in qc.parameters}
    return qc.assign_parameters(params)

def build_qaoa_100():
    n = 100
    qc = QuantumCircuit(n)
    rng = np.random.default_rng(42)
    edges = [(i, (i + 1) % n) for i in range(n)]
    edges += [(rng.integers(0, n), rng.integers(0, n)) for _ in range(n)]
    edges = [(u, v) for u, v in edges if u != v]
    for layer in range(3):
        gamma = 0.5 + layer * 0.1
        beta = 0.3 + layer * 0.05
        for u, v in edges:
            qc.cx(u, v)
            qc.rz(2 * gamma, v)
            qc.cx(u, v)
        qc.rx(2 * beta, range(n))
    qc.measure_all()
    return qc

def build_bv_100():
    n = 100
    s = "1" * (n - 1)
    qc = QuantumCircuit(n)
    qc.h(range(n))
    qc.z(n - 1)
    for i, bit in enumerate(reversed(s)):
        if bit == "1":
            qc.cx(i, n - 1)
    qc.h(range(n))
    qc.measure_all()
    return qc

def build_heisenberg_100():
    n = 100
    rows, cols = 10, 10
    qc = QuantumCircuit(n)
    dt = 0.1
    for step in range(5):
        for r in range(rows):
            for c in range(cols):
                q = r * cols + c
                if c + 1 < cols:
                    q2 = r * cols + (c + 1)
                    qc.rxx(dt, q, q2)
                    qc.ryy(dt, q, q2)
                    qc.rzz(dt, q, q2)
                if r + 1 < rows:
                    q2 = (r + 1) * cols + c
                    qc.rxx(dt, q, q2)
                    qc.ryy(dt, q, q2)
                    qc.rzz(dt, q, q2)
    qc.measure_all()
    return qc

def build_grover_50():
    n = 50
    qc = QuantumCircuit(n)
    qc.h(range(n))
    for _ in range(2):
        for start in range(0, n - 4, 5):
            controls = list(range(start, start + 4))
            target = start + 4
            qc.mcx(controls, target)
        for start in range(0, n - 3, 4):
            controls = list(range(start, start + 2))
            target = min(start + 3, n - 1)
            qc.ccx(controls[0], controls[1], target)
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
    adder = CDKMRippleCarryAdder(39, kind="half")
    qc = QuantumCircuit(adder.num_qubits)
    for i in range(0, adder.num_qubits, 3):
        qc.x(i)
    qc.append(adder, range(adder.num_qubits))
    qc.measure_all()
    return qc

def build_random_80():
    qc = random_circuit(80, 40, max_operands=2, seed=42)
    qc.measure_all()
    return qc

def build_ghz_100():
    n = 100
    qc = QuantumCircuit(n)
    qc.h(0)
    for i in range(n - 1):
        qc.cx(i, i + 1)
    qc.measure_all()
    return qc

def build_qpe_50():
    n_counting = 49
    n = n_counting + 1
    qc = QuantumCircuit(n)
    qc.x(n_counting)
    qc.h(range(n_counting))
    theta = np.pi / 3
    for k in range(n_counting):
        qc.cp((2 ** k) * theta, k, n_counting)
    for i in range(n_counting // 2):
        qc.swap(i, n_counting - 1 - i)
    for i in range(n_counting):
        for j in range(i):
            qc.cp(-np.pi / (2 ** (i - j)), j, i)
        qc.h(i)
    qc.measure_all()
    return qc

def build_toffoli_cascade_90():
    n = 90
    qc = QuantumCircuit(n)
    qc.x(0)
    qc.x(1)
    for i in range(0, n - 2, 2):
        qc.ccx(i, i + 1, i + 2)
    for i in range(1, n - 2, 2):
        qc.ccx(i, i + 1, i + 2)
    for i in range(0, n - 2, 3):
        qc.ccx(i, i + 1, min(i + 2, n - 1))
    qc.measure_all()
    return qc


CIRCUITS = {
    "QFT_100": build_qft_100,
    "QV_100": build_qv_100,
    "EfficientSU2_100": build_efsu2_100,
    "QAOA_100": build_qaoa_100,
    "BV_100": build_bv_100,
    "Heisenberg_100": build_heisenberg_100,
    "Grover_50": build_grover_50,
    "Adder_80": build_adder_80,
    "Random_80": build_random_80,
    "GHZ_100": build_ghz_100,
    "QPE_50": build_qpe_50,
    "Toffoli_90": build_toffoli_cascade_90,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_stats(dag):
    """Get basic circuit stats from DAG."""
    size = dag.size()
    depth = dag.depth()
    n2q = sum(1 for node in dag.op_nodes() if len(node.qargs) == 2)
    return {"size": size, "depth": depth, "2q": n2q}


def run_optimization(pre_opt_circuit, backend, use_3q_blocks=False):
    """Run Level 2 optimization stage on a pre-optimized circuit.

    Args:
        pre_opt_circuit: Circuit after init+layout+routing+translation (no optimization).
        backend: Backend for target/basis_gates.
        use_3q_blocks: If True, use CollectMultiQBlocks(max_block_size=3) instead of
                        Collect2qBlocks before ConsolidateBlocks.

    Returns:
        dict with final_stats, optimization time, and iteration count.
    """
    target = backend.target
    basis_gates = list(target.operation_names)

    dag = circuit_to_dag(pre_opt_circuit)
    pre_stats = get_stats(dag)
    prop_set = PropertySet()

    t_start = time.perf_counter()

    # --- Pre-loop: collection + consolidation + synthesis ---
    if use_3q_blocks:
        collector = CollectMultiQBlocks(max_block_size=3)
    else:
        collector = Collect2qBlocks()

    collector.property_set = prop_set
    collector.run(dag)

    # Also collect 1Q runs
    collect_1q = Collect1qRuns()
    collect_1q.property_set = prop_set
    collect_1q.run(dag)

    # ConsolidateBlocks reads property_set["block_list"] from the collector
    consolidate = ConsolidateBlocks(basis_gates=basis_gates, target=target)
    consolidate.property_set = prop_set
    new_dag = consolidate.run(dag)
    if new_dag is not None:
        dag = new_dag

    # UnitarySynthesis
    synth = UnitarySynthesis(basis_gates, target=target)
    synth.property_set = prop_set
    new_dag = synth.run(dag)
    if new_dag is not None:
        dag = new_dag

    post_preloop_stats = get_stats(dag)

    # --- Optimization loop (same for both variants) ---
    loop_passes = [
        ("RemoveIdentityEquivalent", lambda: RemoveIdentityEquivalent(target=target)),
        ("Optimize1qGatesDecomposition", lambda: Optimize1qGatesDecomposition(
            basis=basis_gates, target=target)),
        ("CommutativeCancellation", lambda: CommutativeCancellation(target=target)),
        ("ContractIdleWiresInControlFlow", lambda: ContractIdleWiresInControlFlow()),
    ]

    size_pass = Size(recurse=True)
    depth_pass = Depth(recurse=True)
    fp_size = FixedPoint("size")
    fp_depth = FixedPoint("depth")

    n_iters = 0
    for i in range(20):  # max 20 iterations
        for pname, pfactory in loop_passes:
            p = pfactory()
            p.property_set = prop_set
            new_dag = p.run(dag)
            if new_dag is not None:
                dag = new_dag

        for p in [size_pass, depth_pass, fp_size, fp_depth]:
            p.property_set = prop_set
            p.run(dag)

        n_iters += 1
        if (prop_set.get("size_fixed_point", False) and
                prop_set.get("depth_fixed_point", False)):
            break

    t_elapsed = time.perf_counter() - t_start

    return {
        "pre_stats": pre_stats,
        "post_preloop_stats": post_preloop_stats,
        "final_stats": get_stats(dag),
        "time_ms": t_elapsed * 1000,
        "n_iters": n_iters,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Experiment: 2Q-only vs 3Q block synthesis at Level 2")
    print(f"{'=' * 80}\n")

    results = {}

    for cname, builder in CIRCUITS.items():
        print(f"\n{'=' * 80}")
        print(f"Building {cname}...")

        try:
            circuit = builder()
            print(f"  Built: {circuit.num_qubits}Q, {circuit.size()} gates")
        except Exception as e:
            print(f"  BUILD ERROR: {e}")
            continue

        # Shared pre-optimization: init + layout + routing + translation (no optimization)
        try:
            pm = generate_preset_pass_manager(2, backend=backend)
            pm.optimization = PassManager()  # skip optimization
            t0 = time.perf_counter()
            pre_opt = pm.run(circuit)
            pre_time = (time.perf_counter() - t0) * 1000
            pre_dag = circuit_to_dag(pre_opt)
            pre_stats = get_stats(pre_dag)
            print(f"  Pre-opt ({pre_time:.0f}ms): {pre_stats['size']} gates, "
                  f"{pre_stats['2q']} 2Q, depth {pre_stats['depth']}")
        except Exception as e:
            print(f"  PRE-OPT ERROR: {e}")
            continue

        # Variant A: 2Q blocks only (current default)
        try:
            r_2q = run_optimization(pre_opt, backend, use_3q_blocks=False)
            print(f"  2Q-only: {r_2q['final_stats']['2q']:>6d} 2Q gates, "
                  f"{r_2q['final_stats']['size']:>6d} total, "
                  f"depth {r_2q['final_stats']['depth']:>5d}, "
                  f"{r_2q['time_ms']:.0f}ms ({r_2q['n_iters']} iters)")
            print(f"    After pre-loop: {r_2q['post_preloop_stats']['2q']} 2Q gates")
        except Exception as e:
            print(f"  2Q-ONLY ERROR: {e}")
            import traceback; traceback.print_exc()
            continue

        # Variant B: 3Q blocks
        try:
            r_3q = run_optimization(pre_opt, backend, use_3q_blocks=True)
            print(f"  2Q+3Q:  {r_3q['final_stats']['2q']:>6d} 2Q gates, "
                  f"{r_3q['final_stats']['size']:>6d} total, "
                  f"depth {r_3q['final_stats']['depth']:>5d}, "
                  f"{r_3q['time_ms']:.0f}ms ({r_3q['n_iters']} iters)")
            print(f"    After pre-loop: {r_3q['post_preloop_stats']['2q']} 2Q gates")
        except Exception as e:
            print(f"  3Q ERROR: {e}")
            import traceback; traceback.print_exc()
            continue

        # Delta
        d2q = r_3q['final_stats']['2q'] - r_2q['final_stats']['2q']
        pct = d2q / r_2q['final_stats']['2q'] * 100 if r_2q['final_stats']['2q'] > 0 else 0
        dsz = r_3q['final_stats']['size'] - r_2q['final_stats']['size']
        ddp = r_3q['final_stats']['depth'] - r_2q['final_stats']['depth']
        print(f"  Delta:  {d2q:+6d} 2Q gates ({pct:+.1f}%), "
              f"{dsz:+6d} total, {ddp:+5d} depth")

        results[cname] = {
            "pre_stats": pre_stats,
            "r_2q": r_2q,
            "r_3q": r_3q,
        }

    # --- Summary table ---
    print(f"\n\n{'=' * 80}")
    print("SUMMARY: 2Q-Only vs 3Q Block Synthesis (Level 2, FakeTorino)")
    print(f"{'=' * 80}\n")

    header = (f"{'Circuit':>20s}  {'Pre-Opt 2Q':>10s}  "
              f"{'2Q-Only':>8s}  {'2Q+3Q':>8s}  "
              f"{'Delta':>8s}  {'Delta%':>8s}  "
              f"{'Time 2Q':>8s}  {'Time 3Q':>8s}")
    print(header)
    print("-" * len(header))

    total_2q_only = 0
    total_3q = 0
    total_pre = 0

    for cname in CIRCUITS:
        if cname not in results:
            print(f"{cname:>20s}  {'SKIPPED':>10s}")
            continue

        r = results[cname]
        pre_2q = r["pre_stats"]["2q"]
        final_2q = r["r_2q"]["final_stats"]["2q"]
        final_3q = r["r_3q"]["final_stats"]["2q"]
        delta = final_3q - final_2q
        pct = delta / final_2q * 100 if final_2q > 0 else 0
        t_2q = r["r_2q"]["time_ms"]
        t_3q = r["r_3q"]["time_ms"]

        total_pre += pre_2q
        total_2q_only += final_2q
        total_3q += final_3q

        print(f"{cname:>20s}  {pre_2q:10d}  "
              f"{final_2q:8d}  {final_3q:8d}  "
              f"{delta:+8d}  {pct:+7.1f}%  "
              f"{t_2q:7.0f}ms  {t_3q:7.0f}ms")

    # Totals
    total_delta = total_3q - total_2q_only
    total_pct = total_delta / total_2q_only * 100 if total_2q_only > 0 else 0
    print("-" * len(header))
    print(f"{'TOTAL':>20s}  {total_pre:10d}  "
          f"{total_2q_only:8d}  {total_3q:8d}  "
          f"{total_delta:+8d}  {total_pct:+7.1f}%")

    print(f"\nConclusion: 3Q block synthesis {'REGRESSES' if total_delta > 0 else 'IMPROVES'} "
          f"gate count by {abs(total_delta)} 2Q gates ({abs(total_pct):.1f}%) overall.")


if __name__ == "__main__":
    main()
