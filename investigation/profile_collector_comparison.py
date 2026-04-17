"""Compare Collect2qBlocks vs CollectMultiQBlocks block grouping and synthesis outcomes.

Key question: when CollectMultiQBlocks merges two 2Q interactions into one 3Q block,
does the 3Q synthesis produce fewer CX than two separate 2Q KAK decompositions?

For each circuit:
1. Pre-optimize (layout + routing + translation, no optimization)
2. Run Collect2qBlocks → count blocks, compute KAK CX for each
3. Run CollectMultiQBlocks(max_block_size=3) → count blocks, compute CSD/QSD CX for 3Q, KAK for 2Q
4. Compare total CX under both strategies (with gate guard: only replace if fewer CX)

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_collector_comparison.py
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
from qiskit.transpiler.passes.optimization.collect_2q_blocks import Collect2qBlocks
from qiskit.transpiler.passes.optimization.collect_multiqubit_blocks import (
    CollectMultiQBlocks,
)
from qiskit.converters import circuit_to_dag
from qiskit.passmanager import PropertySet
from qiskit.quantum_info import Operator
from qiskit.synthesis.two_qubit.two_qubit_decompose import two_qubit_cnot_decompose
from qiskit.synthesis import qs_decomposition
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ===========================================================================
# Utilities
# ===========================================================================

def count_cx_2q(U4):
    """CX count for optimal 2Q synthesis (KAK) of a 4×4 unitary."""
    try:
        circ = two_qubit_cnot_decompose(U4)
        return circ.count_ops().get("cx", 0)
    except Exception:
        return 3


def count_cx_qsd(U8):
    """CX count from Qiskit's QSD for an 8×8 unitary."""
    try:
        circ = qs_decomposition(U8)
        ops = circ.count_ops()
        return ops.get("cx", 0) + ops.get("cz", 0)
    except Exception:
        return -1


def block_qubit_set(block, dag):
    """Get sorted list of qubit indices in a block."""
    qubits = set()
    for node in block:
        for bit in node.qargs:
            qubits.add(dag.find_bit(bit).index)
    return sorted(qubits)


def block_2q_count(block):
    """Count 2Q gates in a block."""
    return sum(1 for node in block if len(node.qargs) == 2)


def block_to_circuit(block, dag, n_qubits):
    """Convert a block to a small QuantumCircuit."""
    qubits = block_qubit_set(block, dag)
    qubit_map = {q: i for i, q in enumerate(qubits)}
    qc = QuantumCircuit(n_qubits)
    for node in block:
        gate_qubits = [qubit_map[dag.find_bit(bit).index] for bit in node.qargs]
        qc.append(node.op, gate_qubits)
    return qc


def block_to_unitary(block, dag, n_qubits):
    """Convert a block to a unitary matrix."""
    qc = block_to_circuit(block, dag, n_qubits)
    return Operator(qc).data


# ===========================================================================
# Analysis: what happens to each block under synthesis + gate guard?
# ===========================================================================

def analyze_2q_collector(blocks, dag):
    """Analyze Collect2qBlocks output.

    Each block acts on exactly 2 qubits. KAK synthesis gives 0-3 CX.
    With gate guard: only replace if KAK CX < original CX.

    Returns: (total_original_cx, total_after_synthesis_cx, n_replaced, n_blocks)
    """
    total_orig = 0
    total_synth = 0
    n_replaced = 0

    for block in blocks:
        qubits = block_qubit_set(block, dag)
        n_q = len(qubits)
        orig_cx = block_2q_count(block)

        if n_q == 1:
            # 1Q block — no 2Q gates, nothing to synthesize
            total_orig += orig_cx
            total_synth += orig_cx
            continue

        if n_q != 2:
            # Shouldn't happen with Collect2qBlocks
            total_orig += orig_cx
            total_synth += orig_cx
            continue

        try:
            U = block_to_unitary(block, dag, 2)
            synth_cx = count_cx_2q(U)
        except Exception:
            synth_cx = orig_cx  # fallback: keep original

        # Gate guard: only replace if synthesis is better
        if synth_cx < orig_cx:
            total_orig += orig_cx
            total_synth += synth_cx
            n_replaced += 1
        else:
            total_orig += orig_cx
            total_synth += orig_cx

    return total_orig, total_synth, n_replaced, len(blocks)


def analyze_multi_collector(blocks, dag):
    """Analyze CollectMultiQBlocks(max_block_size=3) output.

    Blocks can be 1Q, 2Q, or 3Q.
    - 2Q blocks: same as Collect2qBlocks (KAK synthesis)
    - 3Q blocks: QSD synthesis (with gate guard)

    Returns: (total_original_cx, total_after_synthesis_cx, n_replaced_2q, n_replaced_3q,
              n_blocks_2q, n_blocks_3q, details)
    """
    total_orig = 0
    total_synth = 0
    n_replaced_2q = 0
    n_replaced_3q = 0
    n_blocks_2q = 0
    n_blocks_3q = 0

    # Track 3Q block details
    details_3q = []

    for block in blocks:
        qubits = block_qubit_set(block, dag)
        n_q = len(qubits)
        orig_cx = block_2q_count(block)

        if n_q == 1:
            total_orig += orig_cx
            total_synth += orig_cx
            continue

        if n_q == 2:
            n_blocks_2q += 1
            try:
                U = block_to_unitary(block, dag, 2)
                synth_cx = count_cx_2q(U)
            except Exception:
                synth_cx = orig_cx

            if synth_cx < orig_cx:
                total_orig += orig_cx
                total_synth += synth_cx
                n_replaced_2q += 1
            else:
                total_orig += orig_cx
                total_synth += orig_cx
            continue

        if n_q == 3:
            n_blocks_3q += 1
            try:
                U = block_to_unitary(block, dag, 3)
                synth_cx = count_cx_qsd(U)
            except Exception:
                synth_cx = -1

            if synth_cx >= 0 and synth_cx < orig_cx:
                total_orig += orig_cx
                total_synth += synth_cx
                n_replaced_3q += 1
                details_3q.append({
                    "orig": orig_cx, "synth": synth_cx,
                    "saved": orig_cx - synth_cx
                })
            else:
                total_orig += orig_cx
                total_synth += orig_cx
                details_3q.append({
                    "orig": orig_cx,
                    "synth": synth_cx if synth_cx >= 0 else "err",
                    "saved": 0
                })
            continue

        # >3Q blocks (shouldn't happen)
        total_orig += orig_cx
        total_synth += orig_cx

    return (total_orig, total_synth, n_replaced_2q, n_replaced_3q,
            n_blocks_2q, n_blocks_3q, details_3q)


# ===========================================================================
# Also count gates NOT in any block (uncollected gates)
# ===========================================================================

def count_uncollected_2q(dag, blocks):
    """Count 2Q gates in the DAG that are NOT in any block."""
    collected_nodes = set()
    for block in blocks:
        for node in block:
            collected_nodes.add(node._node_id)

    uncollected_2q = 0
    for node in dag.op_nodes():
        if node._node_id not in collected_nodes and len(node.qargs) == 2:
            uncollected_2q += 1
    return uncollected_2q


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
    print("=" * 80)
    print("Collect2qBlocks vs CollectMultiQBlocks(max_block_size=3)")
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print("Both strategies use gate guard: only replace block if synthesis CX < original CX")
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

        # Count total 2Q gates in pre-optimized circuit
        dag = circuit_to_dag(pre_opt)
        total_2q_in_dag = sum(1 for n in dag.op_nodes() if len(n.qargs) == 2)
        print(f"  Pre-optimized: {total_2q_in_dag} 2Q gates")

        # --- Strategy A: Collect2qBlocks ---
        t0 = time.perf_counter()
        dag_a = circuit_to_dag(pre_opt)
        ps_a = PropertySet()
        c2q = Collect2qBlocks()
        c2q.property_set = ps_a
        c2q.run(dag_a)
        blocks_a = ps_a.get("block_list", [])

        uncollected_a = count_uncollected_2q(dag_a, blocks_a)
        orig_a, synth_a, replaced_a, n_blocks_a = analyze_2q_collector(blocks_a, dag_a)
        dt_a = time.perf_counter() - t0

        # Total CX after synthesis = synth_a (from blocks) + uncollected
        final_a = synth_a + uncollected_a

        print(f"\n  Strategy A: Collect2qBlocks")
        print(f"    Blocks: {n_blocks_a} (all 2Q)")
        print(f"    Uncollected 2Q gates: {uncollected_a}")
        print(f"    Block 2Q gates: {orig_a} original → {synth_a} after KAK ({replaced_a} replaced)")
        print(f"    Total 2Q: {total_2q_in_dag} → {final_a}")
        print(f"    Time: {dt_a:.2f}s")

        # --- Strategy B: CollectMultiQBlocks(max_block_size=3) ---
        t0 = time.perf_counter()
        dag_b = circuit_to_dag(pre_opt)
        ps_b = PropertySet()
        cmq = CollectMultiQBlocks(max_block_size=3)
        cmq.property_set = ps_b
        cmq.run(dag_b)
        blocks_b = ps_b.get("block_list", [])

        uncollected_b = count_uncollected_2q(dag_b, blocks_b)
        (orig_b, synth_b, rep_2q_b, rep_3q_b,
         n_2q_b, n_3q_b, details_3q) = analyze_multi_collector(blocks_b, dag_b)
        dt_b = time.perf_counter() - t0

        final_b = synth_b + uncollected_b

        # 3Q block stats
        saved_3q = sum(d["saved"] for d in details_3q)
        orig_3q = sum(d["orig"] for d in details_3q)

        print(f"\n  Strategy B: CollectMultiQBlocks(max_block_size=3)")
        print(f"    Blocks: {n_2q_b} 2Q + {n_3q_b} 3Q = {n_2q_b + n_3q_b} total")
        print(f"    Uncollected 2Q gates: {uncollected_b}")
        print(f"    2Q block CX: {replaced_a} → {rep_2q_b} replaced by KAK")
        print(f"    3Q blocks: {n_3q_b} blocks, {orig_3q} original CX, "
              f"{rep_3q_b} replaced by QSD, saves {saved_3q} CX")
        print(f"    Total 2Q: {total_2q_in_dag} → {final_b}")
        print(f"    Time: {dt_b:.2f}s")

        # --- Comparison ---
        delta = final_b - final_a
        pct = 100 * delta / final_a if final_a > 0 else 0
        winner = "B (MultiQ)" if delta < 0 else "A (2Q)" if delta > 0 else "TIE"
        print(f"\n  Comparison: A={final_a}, B={final_b}, delta={delta:+d} ({pct:+.1f}%) → {winner}")

        # Show what 3Q blocks contain
        if details_3q:
            orig_3q_list = [d["orig"] for d in details_3q]
            synth_3q_list = [d["synth"] for d in details_3q if isinstance(d["synth"], int)]
            if synth_3q_list:
                print(f"\n  3Q block detail:")
                print(f"    Original CX: median={np.median(orig_3q_list):.0f}, "
                      f"mean={np.mean(orig_3q_list):.1f}, max={max(orig_3q_list)}")
                print(f"    QSD CX:      median={np.median(synth_3q_list):.0f}, "
                      f"mean={np.mean(synth_3q_list):.1f}, max={max(synth_3q_list)}")
                # Distribution of 3Q savings
                savings_dist = {}
                for d in details_3q:
                    s = d["saved"]
                    savings_dist[s] = savings_dist.get(s, 0) + 1
                print(f"    Savings distribution: {dict(sorted(savings_dist.items()))}")

        summary.append({
            "circuit": cname,
            "total_2q": total_2q_in_dag,
            "final_a": final_a,
            "final_b": final_b,
            "delta": delta,
            "pct": pct,
            "n_2q_a": n_blocks_a,
            "n_2q_b": n_2q_b,
            "n_3q_b": n_3q_b,
            "uncoll_a": uncollected_a,
            "uncoll_b": uncollected_b,
            "saved_3q": saved_3q,
        })

    # Summary table
    print(f"\n\n{'='*90}")
    print("SUMMARY: Collect2qBlocks (A) vs CollectMultiQBlocks (B) with Gate Guard")
    print(f"{'='*90}")
    print(f"{'Circuit':<20} {'Total2Q':>7} {'A final':>7} {'B final':>7} {'Delta':>7} "
          f"{'%':>6} {'A blks':>6} {'B 2Q':>5} {'B 3Q':>5} "
          f"{'A unc':>5} {'B unc':>5} {'3Q save':>7}")
    print("-" * 90)
    for r in summary:
        print(f"{r['circuit']:<20} {r['total_2q']:>7} {r['final_a']:>7} {r['final_b']:>7} "
              f"{r['delta']:>+7} {r['pct']:>+5.1f}% {r['n_2q_a']:>6} "
              f"{r['n_2q_b']:>5} {r['n_3q_b']:>5} "
              f"{r['uncoll_a']:>5} {r['uncoll_b']:>5} {r['saved_3q']:>7}")

    total_a = sum(r["final_a"] for r in summary)
    total_b = sum(r["final_b"] for r in summary)
    total_delta = total_b - total_a
    total_pct = 100 * total_delta / total_a if total_a > 0 else 0
    print("-" * 90)
    print(f"{'TOTAL':<20} {'':>7} {total_a:>7} {total_b:>7} "
          f"{total_delta:>+7} {total_pct:>+5.1f}%")


if __name__ == "__main__":
    main()
