"""Test Pytket-style sequential strategy: 2Q squash then 3Q squash on top.

Compare:
A: Collect2qBlocks → KAK (current Qiskit Level 2)
C: Same as A, then CollectMultiQBlocks(3) → separability + heuristic QSD guard

The 3Q pass uses two optimizations:
1. Separability check (cheap): if block factors as 2Q⊗1Q, split and use KAK
2. Heuristic QSD guard: only run QSD on blocks with >N original CX (configurable)

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_sequential_strategy.py
"""

import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import (
    CDKMRippleCarryAdder,
    EfficientSU2,
    HRSCumulativeMultiplier,
    QFT,
    QuantumVolume,
)
from qiskit.circuit.random import random_circuit
from qiskit.transpiler import PassManager
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.transpiler.passes import (
    ConsolidateBlocks,
    UnitarySynthesis,
    Optimize1qGatesDecomposition,
    Collect1qRuns,
)
from qiskit.transpiler.passes.optimization.collect_2q_blocks import Collect2qBlocks
from qiskit.transpiler.passes.optimization.collect_multiqubit_blocks import (
    CollectMultiQBlocks,
)
from qiskit.converters import circuit_to_dag, dag_to_circuit
from qiskit.passmanager import PropertySet
from qiskit.quantum_info import Operator
from qiskit.synthesis.two_qubit.two_qubit_decompose import two_qubit_cnot_decompose
from qiskit.synthesis import qs_decomposition
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ===========================================================================
# Separability check (from profile_csd_vs_qsd.py)
# ===========================================================================

def _id_coeff(U, V):
    """If U @ V† ≈ w·I, return w. Else None."""
    W = U @ V.conj().T
    w = W[0, 0]
    if np.allclose(W, w * np.eye(4), atol=1e-8):
        return w
    return None


def _separate_0_12(U):
    """If U = W(2×2) ⊗ V(4×4), return (W, V). Else None."""
    u00, u01 = U[:4, :4], U[:4, 4:]
    u10, u11 = U[4:, :4], U[4:, 4:]

    w0000 = _id_coeff(u00, u00)
    if w0000 is None: return None
    w0101 = _id_coeff(u01, u01)
    if w0101 is None: return None

    x0000, x0101 = w0000.real, w0101.real
    if abs(w0000.imag) > 1e-8 or abs(w0101.imag) > 1e-8: return None
    if x0000 < -1e-8 or x0101 < -1e-8: return None
    x0000, x0101 = max(x0000, 0.0), max(x0101, 0.0)

    if x0000 >= x0101:
        if x0000 < 1e-12: return None
        w00 = np.sqrt(x0000)
        V = u00 / w00
        coeffs = [_id_coeff(u00, u01), _id_coeff(u00, u10), _id_coeff(u00, u11)]
        if any(c is None for c in coeffs): return None
        w01 = np.conj(coeffs[0]) / w00
        w10 = np.conj(coeffs[1]) / w00
        w11 = np.conj(coeffs[2]) / w00
    else:
        if x0101 < 1e-12: return None
        w01 = np.sqrt(x0101)
        V = u01 / w01
        coeffs = [_id_coeff(u01, u00), _id_coeff(u01, u10), _id_coeff(u01, u11)]
        if any(c is None for c in coeffs): return None
        w00 = np.conj(coeffs[0]) / w01
        w10 = np.conj(coeffs[1]) / w01
        w11 = np.conj(coeffs[2]) / w01

    W = np.array([[w00, w01], [w10, w11]])
    if np.allclose(U, np.kron(W, V), atol=1e-8):
        return W, V
    return None


_P1 = np.eye(8)[[0, 1, 4, 5, 2, 3, 6, 7]]
_P2 = np.eye(8)[[0, 4, 2, 6, 1, 5, 3, 7]]


def check_separable(U):
    """Check all 3 bipartitions. Returns (partition, V_2q) or None."""
    r = _separate_0_12(U)
    if r: return "0|12", r[1]
    r = _separate_0_12(_P1 @ U @ _P1.T)
    if r: return "1|02", r[1]
    r = _separate_0_12(_P2 @ U @ _P2.T)
    if r: return "2|01", r[1]
    return None


def count_cx_2q(U4):
    """CX count for optimal 2Q synthesis."""
    try:
        circ = two_qubit_cnot_decompose(U4)
        return circ.count_ops().get("cx", 0)
    except Exception:
        return 3


# ===========================================================================
# Run a single 2Q optimization pass
# ===========================================================================

def run_2q_pass(circuit, backend):
    """Run Collect2qBlocks → ConsolidateBlocks → UnitarySynthesis → 1Q opt."""
    target = backend.target
    basis_gates = list(target.operation_names)
    dag = circuit_to_dag(circuit)
    ps = PropertySet()

    c2q = Collect2qBlocks()
    c2q.property_set = ps
    c2q.run(dag)

    c1q = Collect1qRuns()
    c1q.property_set = ps
    c1q.run(dag)

    consolidate = ConsolidateBlocks(basis_gates=basis_gates, target=target)
    consolidate.property_set = ps
    new_dag = consolidate.run(dag)
    if new_dag is not None:
        dag = new_dag

    synth = UnitarySynthesis(basis_gates=basis_gates, target=target)
    synth.property_set = ps
    new_dag = synth.run(dag)
    if new_dag is not None:
        dag = new_dag

    opt1q = Optimize1qGatesDecomposition(basis=basis_gates, target=target)
    opt1q.property_set = ps
    new_dag = opt1q.run(dag)
    if new_dag is not None:
        dag = new_dag

    return dag_to_circuit(dag)


# ===========================================================================
# Simulate 3Q pass with separability + heuristic guard
# ===========================================================================

def run_3q_pass_simulated(circuit, backend, cx_threshold=14):
    """Simulate 3Q optimization on top of 2Q-optimized circuit.

    For each 3Q block:
    1. Compute 8×8 unitary
    2. Separability check (cheap): if separable, compute KAK CX for 2Q part
    3. If not separable AND orig_cx > cx_threshold: run QSD, apply gate guard
    4. Otherwise: skip (too cheap to bother)

    Returns dict with stats.
    """
    dag = circuit_to_dag(circuit)
    ps = PropertySet()

    cmq = CollectMultiQBlocks(max_block_size=3)
    cmq.property_set = ps
    cmq.run(dag)
    blocks = ps.get("block_list", [])

    stats = {
        "n_3q": 0, "n_sep": 0, "n_qsd_attempted": 0, "n_qsd_replaced": 0,
        "cx_saved_sep": 0, "cx_saved_qsd": 0, "n_skipped": 0,
    }

    for block in blocks:
        qubits = set()
        for node in block:
            for bit in node.qargs:
                qubits.add(dag.find_bit(bit).index)
        if len(qubits) != 3:
            continue

        stats["n_3q"] += 1
        orig_cx = sum(1 for node in block if len(node.qargs) == 2)

        # Compute unitary
        try:
            qubit_list = sorted(qubits)
            qubit_map = {q: i for i, q in enumerate(qubit_list)}
            qc = QuantumCircuit(3)
            for node in block:
                gate_qubits = [qubit_map[dag.find_bit(bit).index] for bit in node.qargs]
                qc.append(node.op, gate_qubits)
            U = Operator(qc).data
        except Exception:
            continue

        # Step 1: separability check (cheap)
        sep = check_separable(U)
        if sep is not None:
            partition, V_2q = sep
            kak_cx = count_cx_2q(V_2q)
            if kak_cx < orig_cx:
                stats["n_sep"] += 1
                stats["cx_saved_sep"] += orig_cx - kak_cx
            continue

        # Step 2: heuristic guard — only try QSD if block is large enough
        if orig_cx <= cx_threshold:
            stats["n_skipped"] += 1
            continue

        # Step 3: run QSD with gate guard
        stats["n_qsd_attempted"] += 1
        try:
            circ_qsd = qs_decomposition(U)
            qsd_cx = circ_qsd.count_ops().get("cx", 0) + circ_qsd.count_ops().get("cz", 0)
        except Exception:
            continue

        if qsd_cx < orig_cx:
            stats["n_qsd_replaced"] += 1
            stats["cx_saved_qsd"] += orig_cx - qsd_cx

    return stats


def count_2q_gates(circuit):
    dag = circuit_to_dag(circuit)
    return sum(1 for n in dag.op_nodes() if len(n.qargs) == 2)


# ===========================================================================
# Circuit Builders
# ===========================================================================

def build_qft_100():
    return QFT(100, name="QFT_100")

def build_qv_100():
    return QuantumVolume(100, 100, seed=12345)

def build_qaoa_100():
    rng = np.random.default_rng(42)
    n = 100
    qc = QuantumCircuit(n)
    for i in range(n):
        qc.h(i)
    for _ in range(3):
        for _ in range(3 * n):
            i, j = rng.choice(n, size=2, replace=False)
            qc.rzz(rng.uniform(0, np.pi), int(i), int(j))
        for i in range(n):
            qc.rx(rng.uniform(0, np.pi), i)
    qc.measure_all()
    return qc

def build_multiplier_10():
    return HRSCumulativeMultiplier(10, name="Multiplier_10")

def build_cdkm_adder_40():
    return CDKMRippleCarryAdder(40, kind="half", name="CDKM_Adder_40")

def build_random_100():
    return random_circuit(100, 100, max_operands=2, seed=42)

def build_toffoli_90():
    qc = QuantumCircuit(90, name="Toffoli_90")
    for i in range(0, 87, 3):
        qc.ccx(i, i + 1, i + 2)
    for i in range(1, 88, 3):
        qc.ccx(i, i + 1, i + 2)
    return qc

def build_efficientsu2_100():
    return EfficientSU2(100, reps=3, entanglement="linear")


CIRCUITS = {
    "QFT_100": build_qft_100,
    "QV_100": build_qv_100,
    "QAOA_100": build_qaoa_100,
    "Random_100": build_random_100,
    "Toffoli_90": build_toffoli_90,
    "Multiplier_10": build_multiplier_10,
    "CDKM_Adder_40": build_cdkm_adder_40,
    "EfficientSU2_100": build_efficientsu2_100,
}


# ===========================================================================
# Main
# ===========================================================================

def main():
    backend = FakeTorino()
    CX_THRESHOLD = 14  # only attempt QSD on blocks with > this many CX

    print("=" * 80)
    print("Sequential Strategy: 2Q squash + 3Q squash (separability + heuristic guard)")
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"3Q heuristic: skip QSD for blocks with ≤{CX_THRESHOLD} CX")
    print("=" * 80)

    summary = []

    for cname, builder in CIRCUITS.items():
        print(f"\n{'='*70}")
        print(f"Circuit: {cname}")

        try:
            circuit = builder()
            print(f"  Built: {circuit.num_qubits}Q, {circuit.size()} gates")
        except Exception as e:
            print(f"  BUILD ERROR: {e}")
            continue

        # Pre-optimize
        try:
            pm = generate_preset_pass_manager(2, backend=backend)
            pm.optimization = PassManager()
            pre_opt = pm.run(circuit)
        except Exception as e:
            print(f"  TRANSPILE ERROR: {e}")
            continue

        cx_pre = count_2q_gates(pre_opt)

        # Step 1: 2Q pass (current Qiskit)
        t0 = time.perf_counter()
        after_2q = run_2q_pass(pre_opt, backend)
        dt_2q = time.perf_counter() - t0
        cx_after_2q = count_2q_gates(after_2q)

        # Step 2: 3Q pass with separability + heuristic guard
        t0 = time.perf_counter()
        s = run_3q_pass_simulated(after_2q, backend, cx_threshold=CX_THRESHOLD)
        dt_3q = time.perf_counter() - t0

        cx_saved = s["cx_saved_sep"] + s["cx_saved_qsd"]
        cx_final = cx_after_2q - cx_saved

        print(f"  Pre-optimized:     {cx_pre:>6} 2Q gates")
        print(f"  After 2Q (KAK):    {cx_after_2q:>6} 2Q gates  ({dt_2q:.1f}s)")
        print(f"  After 2Q + 3Q:     {cx_final:>6} 2Q gates  (+{dt_3q:.1f}s)")
        print(f"    3Q blocks:       {s['n_3q']}")
        print(f"    Separable:       {s['n_sep']} blocks, saved {s['cx_saved_sep']} CX")
        print(f"    QSD attempted:   {s['n_qsd_attempted']} blocks (>{CX_THRESHOLD} CX)")
        print(f"    QSD replaced:    {s['n_qsd_replaced']} blocks, saved {s['cx_saved_qsd']} CX")
        print(f"    Skipped (≤{CX_THRESHOLD}):  {s['n_skipped']} blocks")
        pct = 100 * cx_saved / cx_after_2q if cx_after_2q > 0 else 0
        print(f"    Total saved:     {cx_saved} CX ({pct:.2f}%)")

        summary.append({
            "circuit": cname,
            "cx_pre": cx_pre,
            "cx_2q": cx_after_2q,
            "cx_final": cx_final,
            "cx_saved": cx_saved,
            "cx_sep": s["cx_saved_sep"],
            "cx_qsd": s["cx_saved_qsd"],
            "n_3q": s["n_3q"],
            "n_sep": s["n_sep"],
            "n_attempted": s["n_qsd_attempted"],
            "n_replaced": s["n_qsd_replaced"],
            "n_skipped": s["n_skipped"],
            "dt_2q": dt_2q,
            "dt_3q": dt_3q,
        })

    # Summary table
    print(f"\n\n{'='*100}")
    print("SUMMARY")
    print(f"{'='*100}")
    print(f"{'Circuit':<20} {'After2Q':>7} {'Final':>7} {'Saved':>6} {'%':>6} "
          f"{'Sep':>5} {'QSD':>5} {'3Q blk':>6} {'Tried':>5} {'Skip':>5} "
          f"{'t_2Q':>5} {'t_3Q':>5}")
    print("-" * 100)
    for r in summary:
        pct = 100 * r["cx_saved"] / r["cx_2q"] if r["cx_2q"] > 0 else 0
        print(f"{r['circuit']:<20} {r['cx_2q']:>7} {r['cx_final']:>7} "
              f"{r['cx_saved']:>6} {pct:>5.2f}% "
              f"{r['n_sep']:>5} {r['n_replaced']:>5} {r['n_3q']:>6} "
              f"{r['n_attempted']:>5} {r['n_skipped']:>5} "
              f"{r['dt_2q']:>4.1f}s {r['dt_3q']:>4.1f}s")

    total_2q = sum(r["cx_2q"] for r in summary)
    total_final = sum(r["cx_final"] for r in summary)
    total_saved = sum(r["cx_saved"] for r in summary)
    total_sep = sum(r["cx_sep"] for r in summary)
    total_qsd = sum(r["cx_qsd"] for r in summary)
    total_attempted = sum(r["n_attempted"] for r in summary)
    print("-" * 100)
    pct = 100 * total_saved / total_2q if total_2q > 0 else 0
    print(f"{'TOTAL':<20} {total_2q:>7} {total_final:>7} "
          f"{total_saved:>6} {pct:>5.2f}%")
    print(f"\n  Savings breakdown: separability={total_sep} CX, "
          f"QSD={total_qsd} CX (from {total_attempted} attempts)")


if __name__ == "__main__":
    main()
