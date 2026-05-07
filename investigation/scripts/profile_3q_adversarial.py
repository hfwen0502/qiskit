"""Profile 3-qubit block statistics on adversarial circuits.

Tests whether circuits with highly localized multi-qubit structure
(cascading Toffolis, repeated controlled operations, arithmetic circuits)
produce denser 3Q blocks than the standard benchmarks.

Motivation: Our 12 benchmark circuits all have median 2-12 CX per 3Q block
on heavy-hex, below the 14 CX theoretical minimum for 3Q synthesis. But
those circuits are all built from 1Q/2Q gates and may not represent
worst-case scenarios.

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    ~/.venv/bin/python investigation/profile_3q_adversarial.py
"""

import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import (
    CDKMRippleCarryAdder,
    VBERippleCarryAdder,
    HRSCumulativeMultiplier,
)
from qiskit.transpiler import PassManager
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.transpiler.passes.optimization.collect_2q_blocks import Collect2qBlocks
from qiskit.transpiler.passes.optimization.collect_multiqubit_blocks import (
    CollectMultiQBlocks,
)
from qiskit.converters import circuit_to_dag
from qiskit.passmanager import PropertySet
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Adversarial circuit builders
# ---------------------------------------------------------------------------

def build_deep_toffoli_chain_60():
    """Overlapping CCX gates on sliding 3-qubit windows, multiple sweeps.

    ccx(0,1,2), ccx(1,2,3), ccx(2,3,4), ... — adjacent CCX gates share 2 qubits,
    so after routing they should merge into dense 3Q blocks.
    """
    n = 60
    qc = QuantumCircuit(n)
    qc.x(0)
    qc.x(1)
    # 4 sweeps to create depth
    for sweep in range(4):
        for i in range(0, n - 2):
            qc.ccx(i, i + 1, i + 2)
    qc.measure_all()
    return qc


def build_repeated_toffoli_same_qubits_60():
    """Many CCX gates on the exact same 3 qubits with rotations between.

    Tests whether deep interaction on a fixed 3Q neighborhood creates
    blocks exceeding the synthesis threshold.
    """
    n = 60
    qc = QuantumCircuit(n)
    # Create 10 groups of 3 qubits, each with deep Toffoli sequences
    for group_start in range(0, min(30, n - 2), 3):
        q0, q1, q2 = group_start, group_start + 1, group_start + 2
        for _ in range(15):  # 15 Toffolis per group
            qc.ccx(q0, q1, q2)
            qc.rz(0.1, q2)
            qc.rx(0.2, q0)
    qc.measure_all()
    return qc


def build_cdkm_adder_40():
    """CDKM Ripple Carry Adder — real arithmetic circuit with cascading
    MAJ/UMA stages built from CCX+CX on (a[i], b[i], carry).
    """
    num_state_qubits = 40
    adder = CDKMRippleCarryAdder(num_state_qubits, kind="half")
    qc = QuantumCircuit(adder.num_qubits)
    # Initialize inputs
    for i in range(num_state_qubits):
        qc.h(i)
    qc.compose(adder, inplace=True)
    qc.measure_all()
    return qc


def build_vbe_adder_40():
    """VBE Ripple Carry Adder — alternative adder with different Toffoli structure."""
    num_state_qubits = 20  # VBE uses ancillas, so keep smaller
    adder = VBERippleCarryAdder(num_state_qubits, kind="half")
    qc = QuantumCircuit(adder.num_qubits)
    for i in range(num_state_qubits):
        qc.h(i)
    qc.compose(adder, inplace=True)
    qc.measure_all()
    return qc


def build_multiplier_10():
    """HRS Cumulative Multiplier — controlled adder chains creating
    many overlapping 3Q neighborhoods.
    """
    num_state_qubits = 10
    mult = HRSCumulativeMultiplier(num_state_qubits)
    qc = QuantumCircuit(mult.num_qubits)
    for i in range(num_state_qubits * 2):
        qc.h(i)
    qc.compose(mult, inplace=True)
    qc.measure_all()
    return qc


def build_mcx_cascade_60():
    """Multiple MCX gates with overlapping control qubits.

    mcx([0,1,2,3], 4), mcx([1,2,3,4], 5), ... — MCX decomposes into
    Toffoli chains, creating dense local interactions.
    """
    n = 60
    qc = QuantumCircuit(n)
    qc.h(range(n))
    # Sliding window of 4-control MCX gates
    for i in range(0, n - 4, 2):
        qc.mcx(list(range(i, i + 4)), i + 4)
    qc.measure_all()
    return qc


def build_stabilizer_syndrome_60():
    """Repeated CNOT patterns between data and ancilla qubits.

    Models repetition code syndrome extraction:
    data qubits d0..d29, ancilla qubits a0..a29
    Each round: cx(d[i], a[i]), cx(d[i+1], a[i]) for all i.
    Many rounds to create depth.
    """
    n_data = 30
    n_ancilla = 30
    n = n_data + n_ancilla
    qc = QuantumCircuit(n)
    # Initialize data qubits
    qc.h(range(n_data))
    # 10 rounds of syndrome extraction
    for _round in range(10):
        for i in range(n_ancilla):
            # Each ancilla checks two adjacent data qubits
            d1 = i
            d2 = min(i + 1, n_data - 1)
            a = n_data + i
            qc.cx(d1, a)
            qc.cx(d2, a)
        # Reset ancillas (using X gates as proxy since reset may not be collected)
        for i in range(n_ancilla):
            qc.x(n_data + i)
    qc.measure_all()
    return qc


def build_controlled_rotation_cascade_60():
    """Repeated CRZ gates on overlapping pairs, interleaved.

    crz(q0,q1), crz(q1,q2), crz(q0,q2) in multiple layers.
    Note: parameterized gates ARE collected by CollectMultiQBlocks
    as long as they are unitary (CRZ is unitary).
    """
    n = 60
    qc = QuantumCircuit(n)
    qc.h(range(n))
    # 8 layers of overlapping controlled rotations
    for layer in range(8):
        theta = 0.1 * (layer + 1)
        for i in range(0, n - 2, 3):
            qc.crz(theta, i, i + 1)
            qc.crz(theta, i + 1, i + 2)
            qc.crz(theta, i, i + 2)
    qc.measure_all()
    return qc


CIRCUITS = {
    "DeepToffoliChain_60": build_deep_toffoli_chain_60,
    "RepeatedToffoli_60": build_repeated_toffoli_same_qubits_60,
    "CDKM_Adder_40": build_cdkm_adder_40,
    "VBE_Adder_20": build_vbe_adder_40,
    "Multiplier_10": build_multiplier_10,
    "MCX_Cascade_60": build_mcx_cascade_60,
    "StabilizerSyndrome_60": build_stabilizer_syndrome_60,
    "ControlledRotation_60": build_controlled_rotation_cascade_60,
}


# ---------------------------------------------------------------------------
# Block analysis utilities (same as profile_3q_blocks.py)
# ---------------------------------------------------------------------------

def analyze_blocks(block_list, dag):
    """Classify and analyze blocks by qubit count."""
    result = {1: [], 2: [], 3: []}
    for block in block_list:
        qubit_set = set()
        n_2q = 0
        for node in block:
            qubit_indices = {dag.find_bit(bit).index for bit in node.qargs}
            qubit_set |= qubit_indices
            if len(node.qargs) == 2:
                n_2q += 1
        n_qubits = len(qubit_set)
        if n_qubits > 3:
            continue
        result[n_qubits].append({
            "gates": len(block),
            "2q_gates": n_2q,
            "qubits": n_qubits,
            "qubit_set": qubit_set,
        })
    return result


def print_block_summary(label, blocks_by_nq):
    """Print summary statistics for blocks grouped by qubit count."""
    for nq in [1, 2, 3]:
        blocks = blocks_by_nq[nq]
        if not blocks:
            print(f"  {label} {nq}Q blocks: 0")
            continue
        sizes = [b["gates"] for b in blocks]
        cx_counts = [b["2q_gates"] for b in blocks]
        print(f"  {label} {nq}Q blocks: {len(blocks)}, "
              f"avg={np.mean(sizes):.1f} gates, "
              f"max={max(sizes)} gates, "
              f"median={np.median(sizes):.0f} gates")
        if nq >= 2:
            print(f"    2Q gates: avg={np.mean(cx_counts):.1f}, "
                  f"max={max(cx_counts)}, median={np.median(cx_counts):.0f}")
        if nq == 3:
            over_20 = sum(1 for b in blocks if b["2q_gates"] > 20)
            over_15 = sum(1 for b in blocks if b["2q_gates"] > 15)
            over_14 = sum(1 for b in blocks if b["2q_gates"] > 14)
            over_10 = sum(1 for b in blocks if b["2q_gates"] > 10)
            print(f"    >20 2Q gates (QSD break-even): "
                  f"{over_20}/{len(blocks)} ({100*over_20/len(blocks):.1f}%)")
            print(f"    >15 2Q gates (near-optimal break-even): "
                  f"{over_15}/{len(blocks)} ({100*over_15/len(blocks):.1f}%)")
            print(f"    >14 2Q gates (theoretical minimum): "
                  f"{over_14}/{len(blocks)} ({100*over_14/len(blocks):.1f}%)")
            print(f"    >10 2Q gates: "
                  f"{over_10}/{len(blocks)} ({100*over_10/len(blocks):.1f}%)")
            # CX distribution buckets
            cx_bins = [(1, 5), (6, 10), (11, 14), (15, 20), (21, 50), (51, 9999)]
            dist = []
            for lo, hi in cx_bins:
                count = sum(1 for b in blocks if lo <= b["2q_gates"] <= hi)
                if count > 0:
                    label_str = f"{lo}-{hi}" if hi < 9999 else f"{lo}+"
                    dist.append(f"{label_str}: {count}")
            if dist:
                print(f"    2Q gate distribution: {', '.join(dist)}")


# ---------------------------------------------------------------------------
# Main profiling
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Profiling 3Q block statistics on {len(CIRCUITS)} adversarial circuits")
    print(f"Goal: find circuits that produce 3Q blocks with >14 CX gates")
    print(f"{'='*70}\n")

    summary_rows = []

    for cname, builder in CIRCUITS.items():
        print(f"{'='*70}")
        print(f"Building {cname}...")

        try:
            t0 = time.perf_counter()
            circuit = builder()
            build_time = time.perf_counter() - t0
            print(f"  Built in {build_time*1000:.0f}ms: {circuit.num_qubits}Q, "
                  f"{circuit.size()} gates")
        except Exception as e:
            print(f"  BUILD ERROR: {e}")
            continue

        if circuit.num_qubits > backend.num_qubits:
            print(f"  SKIP: {circuit.num_qubits}Q > {backend.num_qubits}Q backend")
            continue

        # Pre-optimize at level 2 (skip optimization stage)
        print(f"  Pre-optimizing (level 2, skip optimization)...")
        try:
            pm = generate_preset_pass_manager(2, backend=backend)
            pm.optimization = PassManager()
            t0 = time.perf_counter()
            pre_opt = pm.run(circuit)
            pre_time = time.perf_counter() - t0
        except Exception as e:
            print(f"  PRE-OPT ERROR: {e}")
            continue

        dag = circuit_to_dag(pre_opt)
        total_gates = dag.size()
        total_2q = sum(1 for node in dag.op_nodes() if len(node.qargs) == 2)
        total_depth = dag.depth()
        print(f"  Post-routing: {total_gates} gates, {total_2q} 2Q, depth={total_depth}, "
              f"time={pre_time*1000:.0f}ms")

        # --- CollectMultiQBlocks(max_block_size=3) ---
        prop_set_3q = PropertySet()
        collector_3q = CollectMultiQBlocks(max_block_size=3)
        collector_3q.property_set = prop_set_3q
        t0 = time.perf_counter()
        collector_3q.run(dag)
        t_3q = time.perf_counter() - t0
        blocks_3q = prop_set_3q.get("block_list", [])
        analyzed_3q = analyze_blocks(blocks_3q, dag)

        print(f"\n  CollectMultiQBlocks(max_block_size=3) ({t_3q*1000:.1f}ms):")
        print_block_summary("  ", analyzed_3q)

        n_3q_blocks = len(analyzed_3q[3])
        if n_3q_blocks > 0:
            total_3q_2q_gates = sum(b["2q_gates"] for b in analyzed_3q[3])
            max_3q_2q = max(b["2q_gates"] for b in analyzed_3q[3])
            median_3q_2q = np.median([b["2q_gates"] for b in analyzed_3q[3]])
            avg_3q_2q = np.mean([b["2q_gates"] for b in analyzed_3q[3]])
            over_20 = sum(1 for b in analyzed_3q[3] if b["2q_gates"] > 20)
            over_15 = sum(1 for b in analyzed_3q[3] if b["2q_gates"] > 15)
            over_14 = sum(1 for b in analyzed_3q[3] if b["2q_gates"] > 14)
        else:
            total_3q_2q_gates = max_3q_2q = over_20 = over_15 = over_14 = 0
            median_3q_2q = avg_3q_2q = 0

        pct = 100 * total_3q_2q_gates / total_2q if total_2q > 0 else 0
        print(f"\n  Summary:")
        print(f"    3Q blocks: {n_3q_blocks}")
        print(f"    2Q gates in 3Q blocks: {total_3q_2q_gates} / {total_2q} ({pct:.1f}%)")
        print(f"    3Q blocks >20 CX: {over_20}, >15 CX: {over_15}, >14 CX: {over_14}")

        summary_rows.append({
            "name": cname,
            "qubits": circuit.num_qubits,
            "post_route_2q": total_2q,
            "n_3q_blocks": n_3q_blocks,
            "max_3q_2q": max_3q_2q,
            "median_3q_2q": median_3q_2q,
            "avg_3q_2q": avg_3q_2q,
            "over_20": over_20,
            "over_15": over_15,
            "over_14": over_14,
        })
        print()

    # --- Final summary table ---
    print(f"\n{'='*70}")
    print("SUMMARY: Adversarial 3Q Block Statistics (FakeTorino, Heavy-Hex)")
    print(f"{'='*70}\n")

    hdr = (f"{'Circuit':>25s}  {'Post-Route':>10s}  {'3Q Blks':>8s}  "
           f"{'Median':>7s}  {'Avg':>7s}  {'Max':>5s}  "
           f"{'>20CX':>6s}  {'>15CX':>6s}  {'>14CX':>6s}")
    sub = (f"{'':>25s}  {'2Q Gates':>10s}  {'':>8s}  "
           f"{'2Q':>7s}  {'2Q':>7s}  {'2Q':>5s}  "
           f"{'':>6s}  {'':>6s}  {'':>6s}")
    print(hdr)
    print(sub)
    print("-" * 100)

    for r in summary_rows:
        print(f"{r['name']:>25s}  {r['post_route_2q']:10d}  {r['n_3q_blocks']:8d}  "
              f"{r['median_3q_2q']:7.0f}  {r['avg_3q_2q']:7.1f}  {r['max_3q_2q']:5d}  "
              f"{r['over_20']:6d}  {r['over_15']:6d}  {r['over_14']:6d}")

    print("-" * 100)
    total_3q = sum(r["n_3q_blocks"] for r in summary_rows)
    total_over20 = sum(r["over_20"] for r in summary_rows)
    total_over15 = sum(r["over_15"] for r in summary_rows)
    total_over14 = sum(r["over_14"] for r in summary_rows)
    print(f"{'TOTAL':>25s}  {'':>10s}  {total_3q:8d}  "
          f"{'':>7s}  {'':>7s}  {'':>5s}  "
          f"{total_over20:6d}  {total_over15:6d}  {total_over14:6d}")

    print(f"\nConclusion:")
    if total_over14 == 0:
        print(f"  No 3Q blocks exceed 14 CX even on adversarial circuits.")
        print(f"  The topology constraint (heavy-hex degree 2-3) limits block density")
        print(f"  regardless of circuit structure. 3Q synthesis cannot help.")
    else:
        pct14 = 100 * total_over14 / total_3q if total_3q > 0 else 0
        print(f"  {total_over14}/{total_3q} blocks ({pct14:.1f}%) exceed 14 CX.")
        print(f"  These blocks could benefit from near-optimal 3Q synthesis.")
        print(f"  Further investigation needed to assess if this is worth pursuing.")


if __name__ == "__main__":
    main()
