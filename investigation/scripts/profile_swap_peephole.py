"""
Profile SWAP+gate adjacency patterns in post-routing circuits.

Measures how many CX gates could be saved by exploiting SWAP identities:
1. SWAP adjacent to CX on same qubits → save 2 CX (absorption)
2. SWAP-SWAP on same qubits → save 6 CX (full cancellation)
3. CZ adjacent to SWAP → can commute through (enables further optimization)

Usage:
    python investigation/profile_swap_peephole.py
"""

import time
from collections import defaultdict

from qiskit import QuantumCircuit
from qiskit.circuit.library import EfficientSU2, QuantumVolume
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.providers.fake_provider import GenericBackendV2


def get_test_circuits():
    """Generate representative circuits that produce many SWAPs after routing."""
    circuits = {}

    # Linear chain circuits on heavy-hex → lots of SWAPs
    for n in [20, 50, 100]:
        qc = EfficientSU2(n, entanglement="linear", reps=2)
        qc.measure_all()
        circuits[f"su2_linear_{n}q"] = qc

    # Circular entanglement → odd ring on bipartite = worst case
    for n in [20, 50]:
        qc = EfficientSU2(n, entanglement="circular", reps=2)
        qc.measure_all()
        circuits[f"su2_circular_{n}q"] = qc

    # Quantum Volume → dense random connectivity
    for n in [10, 20]:
        qc = QuantumVolume(n, depth=5, seed=42)
        qc.measure_all()
        circuits[f"qv_{n}q"] = qc

    return circuits


def find_swap_cx_patterns(dag):
    """Scan a DAGCircuit for SWAP+CX adjacency patterns.

    Returns counts of:
    - swap_cx_adjacent: CX immediately before/after SWAP on same qubits
    - swap_swap: consecutive SWAPs on same qubits
    - total_swaps: total SWAP gates in the circuit
    - total_cx: total CX gates
    """
    from qiskit.dagcircuit import DAGOpNode

    stats = {
        "total_cx": 0,
        "total_swaps": 0,
        "swap_cx_adjacent": 0,
        "swap_swap_adjacent": 0,
        "potential_cx_savings": 0,
    }

    for node in dag.op_nodes():
        if node.op.name == "cx":
            stats["total_cx"] += 1
        elif node.op.name == "swap":
            stats["total_swaps"] += 1

    # Check each SWAP for adjacent CX or SWAP on same qubits
    for node in dag.op_nodes():
        if node.op.name != "swap":
            continue

        swap_qubits = set(node.qargs)

        # Check successors
        for succ in dag.successors(node):
            if not isinstance(succ, DAGOpNode):
                continue
            if succ.op.name == "cx" and set(succ.qargs) == swap_qubits:
                stats["swap_cx_adjacent"] += 1
                stats["potential_cx_savings"] += 2  # SWAP+CX = 2 CX instead of 4
            elif succ.op.name == "swap" and set(succ.qargs) == swap_qubits:
                stats["swap_swap_adjacent"] += 1
                stats["potential_cx_savings"] += 6  # SWAP+SWAP = 0 instead of 6

        # Check predecessors
        for pred in dag.predecessors(node):
            if not isinstance(pred, DAGOpNode):
                continue
            if pred.op.name == "cx" and set(pred.qargs) == swap_qubits:
                stats["swap_cx_adjacent"] += 1
                stats["potential_cx_savings"] += 2

    # Each pair counted twice (once from each side), correct
    stats["swap_cx_adjacent"] //= 1  # actually each is unique direction
    # Actually predecessors of SWAP A won't be successors of SWAP A,
    # so no double counting. But CX before SWAP and CX after SWAP are
    # separate opportunities.

    return stats


def find_swap_cx_patterns_decomposed(circuit):
    """Scan for patterns in the decomposed circuit (SWAPs → 3 CX).

    After SWAP decomposition, look for CX-CX cancellation opportunities
    at SWAP boundaries.
    """
    from qiskit.converters import circuit_to_dag
    from qiskit.dagcircuit import DAGOpNode

    dag = circuit_to_dag(circuit)

    stats = {
        "total_cx": 0,
        "cx_cx_same_qubits_adjacent": 0,
        "potential_cx_savings": 0,
    }

    for node in dag.op_nodes():
        if node.op.name == "cx":
            stats["total_cx"] += 1

    # Find adjacent CX pairs on same qubits (candidates for cancellation)
    for node in dag.op_nodes():
        if node.op.name != "cx":
            continue

        node_qubits = tuple(node.qargs)

        for succ in dag.successors(node):
            if not isinstance(succ, DAGOpNode):
                continue
            if succ.op.name == "cx" and tuple(succ.qargs) == node_qubits:
                stats["cx_cx_same_qubits_adjacent"] += 1
                stats["potential_cx_savings"] += 2  # pair cancels

    return stats


def main():
    # Use 133-qubit heavy-hex backend (like FakeTorino)
    backend = GenericBackendV2(num_qubits=133, coupling_map=None, seed=42)

    # Try to use FakeTorino for realistic heavy-hex
    try:
        from qiskit_ibm_runtime.fake_provider import FakeTorino
        backend = FakeTorino()
        backend_name = "FakeTorino (133Q heavy-hex)"
    except ImportError:
        backend_name = "GenericBackendV2 (133Q)"

    pm = generate_preset_pass_manager(optimization_level=2, backend=backend)

    circuits = get_test_circuits()

    print("=" * 80)
    print(f" SWAP Peephole Opportunity Analysis — {backend_name}")
    print("=" * 80)
    print()

    # Phase 1: Analyze before SWAP decomposition
    print("Phase 1: Pre-decomposition analysis (SWAP gates visible)")
    print("-" * 80)
    print(f"{'Circuit':<25} {'CX':>6} {'SWAPs':>6} {'SWAP+CX':>8} "
          f"{'SWAP+SWAP':>10} {'Potential':>10} {'% savings':>10}")
    print("-" * 80)

    results = {}
    for name, qc in circuits.items():
        t0 = time.time()

        # Transpile but stop before final decomposition to see SWAPs
        # Use level 2 but with basis gates that include swap
        transpiled = pm.run(qc)
        elapsed = time.time() - t0

        # Convert to DAG and analyze
        from qiskit.converters import circuit_to_dag
        dag = circuit_to_dag(transpiled)

        stats = find_swap_cx_patterns(dag)
        stats["compile_time"] = elapsed
        stats["circuit_depth"] = transpiled.depth()
        results[name] = stats

        pct = (stats["potential_cx_savings"] / max(stats["total_cx"], 1)) * 100

        print(f"{name:<25} {stats['total_cx']:>6} {stats['total_swaps']:>6} "
              f"{stats['swap_cx_adjacent']:>8} {stats['swap_swap_adjacent']:>10} "
              f"{stats['potential_cx_savings']:>10} {pct:>9.1f}%")

    print()

    # Phase 2: Analyze after SWAP decomposition (CX-CX cancellation)
    print("Phase 2: Post-decomposition analysis (SWAPs decomposed to CX)")
    print("-" * 80)
    print(f"{'Circuit':<25} {'Total CX':>10} {'CX-CX adj':>10} "
          f"{'Potential':>10} {'% savings':>10}")
    print("-" * 80)

    for name, qc in circuits.items():
        transpiled = pm.run(qc)

        # Decompose SWAPs to CX
        from qiskit.transpiler.passes import Decompose
        from qiskit.circuit.library import SwapGate

        # Check if there are still SWAP gates
        ops = transpiled.count_ops()
        if "swap" in ops:
            # Decompose swaps
            from qiskit.transpiler import PassManager
            from qiskit.transpiler.passes import BasisTranslator
            from qiskit.circuit.equivalence_library import SessionEquivalenceLibrary
            decompose_pm = PassManager([
                BasisTranslator(SessionEquivalenceLibrary, ["cx", "id", "rz", "sx", "x"])
            ])
            transpiled = decompose_pm.run(transpiled)

        stats2 = find_swap_cx_patterns_decomposed(transpiled)
        pct = (stats2["potential_cx_savings"] / max(stats2["total_cx"], 1)) * 100

        print(f"{name:<25} {stats2['total_cx']:>10} "
              f"{stats2['cx_cx_same_qubits_adjacent']:>10} "
              f"{stats2['potential_cx_savings']:>10} {pct:>9.1f}%")

    print()
    print("=" * 80)
    print("Notes:")
    print("- SWAP+CX adjacent: CX immediately before/after SWAP on same qubits")
    print("  Each saves 2 CX (SWAP+CX = 2 CX instead of 3+1)")
    print("- SWAP+SWAP adjacent: consecutive SWAPs on same qubits → full cancel")
    print("  Each saves 6 CX (two SWAPs = 6 CX → 0)")
    print("- Phase 2 looks at CX-CX cancellation after SWAP decomposition")
    print("=" * 80)


if __name__ == "__main__":
    main()
