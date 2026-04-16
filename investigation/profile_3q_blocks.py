"""Profile 3-qubit block statistics across benchmark circuits.

Runs CollectMultiQBlocks(max_block_size=3) on pre-optimized circuits to answer:
- How many 3Q blocks exist after routing?
- How large are they (gate count, 2Q gate count)?
- Do any exceed the QSD break-even threshold (~20 CX gates)?
- Which circuit families produce the most/largest 3Q blocks?

Also runs Collect2qBlocks for baseline comparison.

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_3q_blocks.py
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
from qiskit.transpiler.passes.optimization.collect_2q_blocks import Collect2qBlocks
from qiskit.transpiler.passes.optimization.collect_multiqubit_blocks import (
    CollectMultiQBlocks,
)
from qiskit.converters import circuit_to_dag
from qiskit.passmanager import PropertySet
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Circuit builders (copied from profile_optimization_loop.py + test_more_circuits.py)
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
            qc.rzz(rng.uniform(0, np.pi), int(i), int(j))
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

def build_grover_50():
    n = 50
    qc = QuantumCircuit(n)
    qc.h(range(n))
    for _ in range(2):
        for start in range(0, n - 4, 5):
            qc.mcx(list(range(start, start + 4)), start + 4)
        for start in range(0, n - 3, 4):
            qc.ccx(start, start + 1, min(start + 3, n - 1))
        qc.h(range(n))
        qc.x(range(n))
        for start in range(0, n - 4, 5):
            qc.mcx(list(range(start, start + 4)), start + 4)
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
    "EfficientSU2_100": build_efficientsu2_100,
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
# Block analysis utilities
# ---------------------------------------------------------------------------

def analyze_blocks(block_list, dag):
    """Classify and analyze blocks by qubit count.

    Returns dict with keys 1, 2, 3 mapping to lists of block stats.
    Each block stat: {gates, 2q_gates, qubits, qubit_set}
    """
    result = {1: [], 2: [], 3: []}

    for block in block_list:
        # Collect all qubits involved in this block
        qubit_set = set()
        n_2q = 0
        for node in block:
            qubit_indices = {dag.find_bit(bit).index for bit in node.qargs}
            qubit_set |= qubit_indices
            if len(node.qargs) == 2:
                n_2q += 1

        n_qubits = len(qubit_set)
        if n_qubits > 3:
            continue  # shouldn't happen with max_block_size=3 but be safe

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
            # Report payoff thresholds
            over_20 = sum(1 for b in blocks if b["2q_gates"] > 20)
            over_15 = sum(1 for b in blocks if b["2q_gates"] > 15)
            over_10 = sum(1 for b in blocks if b["2q_gates"] > 10)
            print(f"    >20 2Q gates (QSD break-even): "
                  f"{over_20}/{len(blocks)} ({100*over_20/len(blocks):.0f}%)")
            print(f"    >15 2Q gates (near-optimal break-even): "
                  f"{over_15}/{len(blocks)} ({100*over_15/len(blocks):.0f}%)")
            print(f"    >10 2Q gates: "
                  f"{over_10}/{len(blocks)} ({100*over_10/len(blocks):.0f}%)")

            # Size distribution
            bins = [(1, 5), (6, 10), (11, 20), (21, 50), (51, 100), (101, 9999)]
            dist = []
            for lo, hi in bins:
                count = sum(1 for b in blocks if lo <= b["gates"] <= hi)
                if count > 0:
                    dist.append(f"{lo}-{hi}: {count}")
            if dist:
                print(f"    Size distribution: {', '.join(dist)}")


# ---------------------------------------------------------------------------
# Main profiling
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Profiling 3Q block statistics on {len(CIRCUITS)} circuits\n")

    # Collect summary data for final table
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

        # --- Collect2qBlocks (baseline) ---
        prop_set_2q = PropertySet()
        collector_2q = Collect2qBlocks()
        collector_2q.property_set = prop_set_2q
        t0 = time.perf_counter()
        collector_2q.run(dag)
        t_2q = time.perf_counter() - t0
        blocks_2q = prop_set_2q.get("block_list", [])
        analyzed_2q = analyze_blocks(blocks_2q, dag)

        print(f"\n  Collect2qBlocks ({t_2q*1000:.1f}ms):")
        print_block_summary("  ", analyzed_2q)

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

        # --- Comparison ---
        n_2q_blocks_baseline = len(analyzed_2q[2])
        n_2q_blocks_multi = len(analyzed_3q[2])
        n_3q_blocks = len(analyzed_3q[3])

        total_3q_gates = sum(b["gates"] for b in analyzed_3q[3])
        total_3q_2q_gates = sum(b["2q_gates"] for b in analyzed_3q[3])
        max_3q_size = max((b["gates"] for b in analyzed_3q[3]), default=0)
        max_3q_2q = max((b["2q_gates"] for b in analyzed_3q[3]), default=0)
        over_20 = sum(1 for b in analyzed_3q[3] if b["2q_gates"] > 20)
        over_15 = sum(1 for b in analyzed_3q[3] if b["2q_gates"] > 15)

        # How many 2Q gates are "captured" in 3Q blocks vs left in 2Q blocks?
        captured_2q_in_3q = total_3q_2q_gates
        captured_2q_in_2q = sum(b["2q_gates"] for b in analyzed_3q[2])
        print(f"\n  Summary:")
        print(f"    2Q blocks (baseline): {n_2q_blocks_baseline}")
        print(f"    2Q blocks (with 3Q collection): {n_2q_blocks_multi}")
        print(f"    3Q blocks: {n_3q_blocks}")
        print(f"    2Q gates in 3Q blocks: {captured_2q_in_3q} / {total_2q} "
              f"({100*captured_2q_in_3q/total_2q:.1f}%)" if total_2q > 0 else "")
        print(f"    3Q blocks >20 CX (QSD payoff): {over_20}")
        print(f"    3Q blocks >15 CX (near-optimal payoff): {over_15}")

        summary_rows.append({
            "name": cname,
            "qubits": circuit.num_qubits,
            "post_route_2q": total_2q,
            "n_2q_blocks": n_2q_blocks_baseline,
            "n_3q_blocks": n_3q_blocks,
            "total_3q_gates": total_3q_gates,
            "total_3q_2q": total_3q_2q_gates,
            "max_3q_size": max_3q_size,
            "max_3q_2q": max_3q_2q,
            "over_20": over_20,
            "over_15": over_15,
        })

        print()

    # --- Final summary table ---
    print(f"\n{'='*70}")
    print("SUMMARY: 3-Qubit Block Statistics")
    print(f"{'='*70}\n")

    hdr = (f"{'Circuit':>20s}  {'Post-Route':>10s}  {'2Q Blks':>8s}  {'3Q Blks':>8s}  "
           f"{'3Q Max':>7s}  {'3Q Max':>7s}  {'CX in':>7s}  {'>20CX':>6s}  {'>15CX':>6s}")
    sub = (f"{'':>20s}  {'2Q Gates':>10s}  {'':>8s}  {'':>8s}  "
           f"{'Gates':>7s}  {'2Q':>7s}  {'3Q Blks':>7s}  {'':>6s}  {'':>6s}")
    print(hdr)
    print(sub)
    print("-" * 100)

    for r in summary_rows:
        print(f"{r['name']:>20s}  {r['post_route_2q']:10d}  {r['n_2q_blocks']:8d}  "
              f"{r['n_3q_blocks']:8d}  {r['max_3q_size']:7d}  {r['max_3q_2q']:7d}  "
              f"{r['total_3q_2q']:7d}  {r['over_20']:6d}  {r['over_15']:6d}")

    # Totals
    print("-" * 100)
    print(f"{'TOTAL':>20s}  {sum(r['post_route_2q'] for r in summary_rows):10d}  "
          f"{sum(r['n_2q_blocks'] for r in summary_rows):8d}  "
          f"{sum(r['n_3q_blocks'] for r in summary_rows):8d}  "
          f"{'':>7s}  {'':>7s}  "
          f"{sum(r['total_3q_2q'] for r in summary_rows):7d}  "
          f"{sum(r['over_20'] for r in summary_rows):6d}  "
          f"{sum(r['over_15'] for r in summary_rows):6d}")

    # Bottom line
    total_3q = sum(r['n_3q_blocks'] for r in summary_rows)
    total_over_20 = sum(r['over_20'] for r in summary_rows)
    total_over_15 = sum(r['over_15'] for r in summary_rows)
    total_2q_gates = sum(r['post_route_2q'] for r in summary_rows)
    total_captured = sum(r['total_3q_2q'] for r in summary_rows)
    print(f"\nBottom line:")
    print(f"  Total 3Q blocks across all circuits: {total_3q}")
    print(f"  3Q blocks with >20 CX (would benefit from QSD): {total_over_20} "
          f"({100*total_over_20/total_3q:.1f}%)" if total_3q > 0 else "  No 3Q blocks found")
    print(f"  3Q blocks with >15 CX (would benefit from near-optimal): {total_over_15} "
          f"({100*total_over_15/total_3q:.1f}%)" if total_3q > 0 else "")
    print(f"  2Q gates captured in 3Q blocks: {total_captured} / {total_2q_gates} "
          f"({100*total_captured/total_2q_gates:.1f}%)" if total_2q_gates > 0 else "")


if __name__ == "__main__":
    main()
