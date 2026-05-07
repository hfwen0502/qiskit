"""Quick test: does pass ordering within the optimization stage matter?

Tests 3 orderings on 6 circuits, single iteration (no loop), Level 3,
all starting from the same pre-optimized circuit.

Orderings:
  A (current):  ConsolidateBlocks → UnitarySynthesis → RemoveIdentityEquivalent →
                Optimize1qGatesDecomposition → CommutativeCancellation
  B (cancel first): CommutativeCancellation → ConsolidateBlocks → UnitarySynthesis →
                     RemoveIdentityEquivalent → Optimize1qGatesDecomposition
  C (light first):  RemoveIdentityEquivalent → Optimize1qGatesDecomposition →
                     CommutativeCancellation → ConsolidateBlocks → UnitarySynthesis

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/test_pass_ordering.py
"""

import time
import warnings
import copy

import numpy as np

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import EfficientSU2, QFT, QuantumVolume
from qiskit.transpiler import PassManager
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.transpiler.passes import (
    ConsolidateBlocks,
    UnitarySynthesis,
    RemoveIdentityEquivalent,
    Optimize1qGatesDecomposition,
    CommutativeCancellation,
    ContractIdleWiresInControlFlow,
)
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit.passmanager import PropertySet
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Circuit builders (same as main profiling script)
# ---------------------------------------------------------------------------

def build_qft_100():
    return QFT(100, name="QFT_100")

def build_qv_100():
    return QuantumVolume(100, 100, seed=12345)

def build_efficientsu2_100():
    return EfficientSU2(100, reps=3, entanglement="linear")

def build_qaoa_100():
    rng = np.random.default_rng(42)
    n = 100
    qc = QuantumCircuit(n)
    for i in range(n):
        qc.h(i)
    for _ in range(3):
        for _ in range(3 * n):
            i, j = rng.choice(n, size=2, replace=False)
            angle = rng.uniform(0, np.pi)
            qc.rzz(angle, int(i), int(j))
        for i in range(n):
            qc.rx(rng.uniform(0, np.pi), i)
    qc.measure_all()
    return qc

def build_bv_100():
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
    n = 100
    side = 10
    qc = QuantumCircuit(n)
    for i in range(n):
        qc.h(i)
    dt = 0.1
    for rep in range(3):
        for i in range(side):
            for j in range(side):
                q = i * side + j
                if j + 1 < side:
                    r = i * side + (j + 1)
                    qc.rxx(dt, q, r)
                    qc.ryy(dt, q, r)
                    qc.rzz(dt, q, r)
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
# Run one pass ordering on a DAG
# ---------------------------------------------------------------------------

def get_stats(dag):
    size = dag.size()
    depth = dag.depth()
    n2q = sum(1 for node in dag.op_nodes() if len(node.qargs) == 2)
    return {"size": size, "depth": depth, "2q": n2q}


def run_ordering(dag, pass_list, prop_set):
    """Run a list of (name, pass) tuples on a DAG. Returns final stats and timing."""
    t0 = time.perf_counter()
    for name, p in pass_list:
        p.property_set = prop_set
        new_dag = p.run(dag)
        if new_dag is not None:
            dag = new_dag
    elapsed = time.perf_counter() - t0
    stats = get_stats(dag)
    stats["time_ms"] = elapsed * 1000
    return stats


def build_passes(backend):
    """Build fresh pass instances."""
    target = backend.target
    basis_gates = list(target.operation_names)
    return {
        "CB": ("ConsolidateBlocks", ConsolidateBlocks(basis_gates=basis_gates, target=target)),
        "US": ("UnitarySynthesis", UnitarySynthesis(basis_gates, target=target)),
        "RI": ("RemoveIdentityEquivalent", RemoveIdentityEquivalent(target=target)),
        "O1": ("Optimize1qGatesDecomposition", Optimize1qGatesDecomposition(basis=basis_gates, target=target)),
        "CC": ("CommutativeCancellation", CommutativeCancellation(target=target)),
    }


ORDERINGS = {
    "A (current)":     ["CB", "US", "RI", "O1", "CC"],
    "B (cancel first)": ["CC", "CB", "US", "RI", "O1"],
    "C (light first)":  ["RI", "O1", "CC", "CB", "US"],
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Testing 3 pass orderings, single iteration, Level 3\n")

    # Collect results: {circuit_name: {ordering_name: stats}}
    all_results = {}

    for cname, builder in CIRCUITS.items():
        print(f"Building {cname}...", end=" ", flush=True)
        circuit = builder()
        print(f"{circuit.num_qubits}Q, {circuit.size()} gates")

        # Pre-optimize (init + layout + routing + translation)
        print(f"  Pre-optimizing...", end=" ", flush=True)
        pm = generate_preset_pass_manager(3, backend=backend)
        pm.optimization = PassManager()
        pre_opt = pm.run(circuit)
        base_dag = circuit_to_dag(pre_opt)
        base_stats = get_stats(base_dag)
        print(f"size={base_stats['size']}, 2q={base_stats['2q']}, depth={base_stats['depth']}")

        all_results[cname] = {"pre_opt": base_stats}

        for oname, order_keys in ORDERINGS.items():
            # Fresh passes and property set for each ordering
            passes = build_passes(backend)
            pass_list = [passes[k] for k in order_keys]
            prop_set = PropertySet()

            # Deep copy the DAG so each ordering starts from the same circuit
            dag_copy = circuit_to_dag(dag_to_circuit(base_dag))

            stats = run_ordering(dag_copy, pass_list, prop_set)
            all_results[cname][oname] = stats
            print(f"  {oname:20s}: 2q={stats['2q']:6d}  size={stats['size']:6d}  "
                  f"depth={stats['depth']:5d}  time={stats['time_ms']:.0f}ms")

    # Summary table
    print(f"\n{'=' * 100}")
    print("SUMMARY: Pass Ordering Comparison (single iteration, Level 3)")
    print(f"{'=' * 100}")

    # 2Q gates
    print(f"\n--- 2Q Gates ---\n")
    header = f"{'Circuit':>20s}  {'Pre-Opt':>8s}"
    for oname in ORDERINGS:
        header += f"  {oname:>20s}"
    header += f"  {'B-A':>6s}  {'C-A':>6s}"
    print(header)
    print("-" * (20 + 8 + 22 * len(ORDERINGS) + 14))

    for cname in CIRCUITS:
        r = all_results[cname]
        row = f"{cname:>20s}  {r['pre_opt']['2q']:8d}"
        vals = []
        for oname in ORDERINGS:
            v = r[oname]["2q"]
            vals.append(v)
            row += f"  {v:20d}"
        diff_ba = vals[1] - vals[0]
        diff_ca = vals[2] - vals[0]
        row += f"  {diff_ba:+6d}  {diff_ca:+6d}"
        print(row)

    # Timing
    print(f"\n--- Optimization Time (ms) ---\n")
    header = f"{'Circuit':>20s}"
    for oname in ORDERINGS:
        header += f"  {oname:>20s}"
    print(header)
    print("-" * (20 + 22 * len(ORDERINGS)))

    for cname in CIRCUITS:
        r = all_results[cname]
        row = f"{cname:>20s}"
        for oname in ORDERINGS:
            row += f"  {r[oname]['time_ms']:20.0f}"
        print(row)

    # Total gates
    print(f"\n--- Total Gates ---\n")
    header = f"{'Circuit':>20s}  {'Pre-Opt':>8s}"
    for oname in ORDERINGS:
        header += f"  {oname:>20s}"
    header += f"  {'B-A':>6s}  {'C-A':>6s}"
    print(header)
    print("-" * (20 + 8 + 22 * len(ORDERINGS) + 14))

    for cname in CIRCUITS:
        r = all_results[cname]
        row = f"{cname:>20s}  {r['pre_opt']['size']:8d}"
        vals = []
        for oname in ORDERINGS:
            v = r[oname]["size"]
            vals.append(v)
            row += f"  {v:20d}"
        diff_ba = vals[1] - vals[0]
        diff_ca = vals[2] - vals[0]
        row += f"  {diff_ba:+6d}  {diff_ca:+6d}"
        print(row)

    # Depth
    print(f"\n--- Circuit Depth ---\n")
    header = f"{'Circuit':>20s}  {'Pre-Opt':>8s}"
    for oname in ORDERINGS:
        header += f"  {oname:>20s}"
    header += f"  {'B-A':>6s}  {'C-A':>6s}"
    print(header)
    print("-" * (20 + 8 + 22 * len(ORDERINGS) + 14))

    for cname in CIRCUITS:
        r = all_results[cname]
        row = f"{cname:>20s}  {r['pre_opt']['depth']:8d}"
        vals = []
        for oname in ORDERINGS:
            v = r[oname]["depth"]
            vals.append(v)
            row += f"  {v:20d}"
        diff_ba = vals[1] - vals[0]
        diff_ca = vals[2] - vals[0]
        row += f"  {diff_ba:+6d}  {diff_ca:+6d}"
        print(row)


if __name__ == "__main__":
    main()
