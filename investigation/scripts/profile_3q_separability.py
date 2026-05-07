"""Profile 3Q block separability: how many are tensor products?

For each 3Q block found by CollectMultiQBlocks, computes the 8x8 unitary
and checks if it's separable as:
  - (2Q ⊗ 1Q) or (1Q ⊗ 2Q): bipartite separable
  - (1Q ⊗ 1Q ⊗ 1Q): fully separable

Also checks: for blocks that ARE entangled, how many CX gates does the
existing 2Q KAK decomposition need after splitting into 2Q+1Q pieces?

Uses SVD-based separability check: reshape 8x8 unitary as 4x2 (or 2x4),
compute SVD, check if rank-1 (single nonzero singular value).

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_3q_separability.py
"""

import time
import warnings

import numpy as np
from scipy.linalg import svd

warnings.filterwarnings("ignore")

from qiskit import QuantumCircuit
from qiskit.circuit.library import CDKMRippleCarryAdder, EfficientSU2, QFT, QuantumVolume
from qiskit.circuit.random import random_circuit
from qiskit.transpiler import PassManager
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.transpiler.passes.optimization.collect_multiqubit_blocks import CollectMultiQBlocks
from qiskit.converters import circuit_to_dag
from qiskit.passmanager import PropertySet
from qiskit.quantum_info import Operator
from qiskit.synthesis.two_qubit.two_qubit_decompose import (
    TwoQubitWeylDecomposition,
    TwoQubitBasisDecomposer,
)
from qiskit_ibm_runtime.fake_provider import FakeTorino


# ---------------------------------------------------------------------------
# Circuit builders (same as profile_3q_blocks.py)
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
# Separability analysis
# ---------------------------------------------------------------------------

FIDELITY_THRESHOLD = 1.0 - 1e-9


def is_bipartite_separable(unitary_8x8, split):
    """Check if an 8x8 unitary is a tensor product U_A ⊗ U_B.

    split: (dim_A, dim_B) e.g. (4, 2) for 2Q ⊗ 1Q or (2, 4) for 1Q ⊗ 2Q.

    Method: Reshape the unitary as (dim_A, dim_B, dim_A, dim_B), then as
    (dim_A^2, dim_B^2) and check if it has rank 1 via SVD.
    """
    dim_a, dim_b = split
    # Reshape: U[i*dim_b + j, k*dim_b + l] -> T[i,j,k,l] -> M[i*k_flat, j*l_flat]
    # Better approach: use the property that U = A ⊗ B iff
    # the matrix U reshaped as (dim_a, dim_b, dim_a, dim_b) then
    # transposed to (dim_a, dim_a, dim_b, dim_b) and reshaped to (dim_a^2, dim_b^2)
    # has rank 1.
    T = unitary_8x8.reshape(dim_a, dim_b, dim_a, dim_b)
    M = T.transpose(0, 2, 1, 3).reshape(dim_a * dim_a, dim_b * dim_b)
    s = svd(M, compute_uv=False)
    # Check rank-1: ratio of first to second singular value
    if s[1] < 1e-10 * s[0]:
        return True
    # Also check via fidelity: if nearly separable
    ratio = s[0] / np.sum(s)
    return ratio > (1.0 - 1e-6)


def analyze_separability(unitary_8x8):
    """Analyze separability of a 3Q unitary.

    Returns dict with:
      - separable_type: "full" (1Q⊗1Q⊗1Q), "bipartite_01_2" (2Q⊗1Q),
                        "bipartite_0_12" (1Q⊗2Q), or "entangled"
      - For bipartite: the 2Q subunitary's CX count via Weyl decomposition
    """
    result = {"separable_type": "entangled", "cx_if_split": None}

    # Check 3 bipartite splits: (01|2), (0|12), (02|1)
    # For qubits labeled 0,1,2 in an 8x8 matrix:
    # Split (01|2): dim_A=4, dim_B=2 — qubits 0,1 vs qubit 2
    # Split (0|12): dim_A=2, dim_B=4 — qubit 0 vs qubits 1,2
    # Split (02|1): requires qubit permutation first

    splits = [
        ("bipartite_01_2", (4, 2)),  # qubits (0,1) | (2)
        ("bipartite_0_12", (2, 4)),  # qubit (0) | qubits (1,2)
    ]

    # For the (02|1) split, we need to permute qubits: swap qubits 1 and 2
    # In the computational basis, this means swapping the middle qubit
    # |q0 q1 q2> -> |q0 q2 q1>
    perm = np.zeros((8, 8), dtype=complex)
    for i in range(8):
        q0 = (i >> 2) & 1
        q1 = (i >> 1) & 1
        q2 = i & 1
        j = (q0 << 2) | (q2 << 1) | q1
        perm[j, i] = 1.0
    U_permuted = perm @ unitary_8x8 @ perm.T

    splits.append(("bipartite_02_1", (4, 2)))  # after permutation: (0,2) | (1)

    for split_name, dims in splits:
        U = U_permuted if split_name == "bipartite_02_1" else unitary_8x8
        if is_bipartite_separable(U, dims):
            result["separable_type"] = split_name

            # Extract the 2Q part and compute its CX count
            dim_a, dim_b = dims
            T = U.reshape(dim_a, dim_b, dim_a, dim_b)
            M = T.transpose(0, 2, 1, 3).reshape(dim_a * dim_a, dim_b * dim_b)
            u_svd, s_svd, vh_svd = svd(M)
            # The 2Q part is from the dominant singular vector
            if dim_a == 4:
                u_2q = u_svd[:, 0].reshape(dim_a, dim_a) * np.sqrt(s_svd[0])
            elif dim_b == 4:
                u_2q = (vh_svd[0, :].reshape(dim_b, dim_b) * np.sqrt(s_svd[0])).T
            else:
                break

            # Normalize to unitary (fix determinant)
            det = np.linalg.det(u_2q)
            if abs(det) > 1e-10:
                u_2q = u_2q / (det ** (1.0/4))

            try:
                weyl = TwoQubitWeylDecomposition(u_2q)
                circ = weyl.circuit(euler_basis='ZYZ', simplify=True, atol=1e-12)
                cx_count = sum(1 for inst in circ if inst.operation.num_qubits == 2)
                result["cx_if_split"] = cx_count
                # Check if 2Q part is also separable (= fully separable)
                if cx_count == 0:
                    result["separable_type"] = "full"
            except Exception:
                result["cx_if_split"] = -1
            break

    return result


def block_to_unitary(block, dag):
    """Convert a block of DAGOpNodes to an 8x8 unitary matrix."""
    # Get ordered qubit indices
    qubit_set = set()
    for node in block:
        for bit in node.qargs:
            qubit_set.add(dag.find_bit(bit).index)
    qubits = sorted(qubit_set)
    assert len(qubits) == 3, f"Expected 3 qubits, got {len(qubits)}"

    # Build a small circuit with the block's gates
    qubit_map = {q: i for i, q in enumerate(qubits)}
    qc = QuantumCircuit(3)
    for node in block:
        gate_qubits = [qubit_map[dag.find_bit(bit).index] for bit in node.qargs]
        qc.append(node.op, gate_qubits)

    return Operator(qc).data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    backend = FakeTorino()
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print(f"Profiling 3Q block separability on {len(CIRCUITS)} circuits\n")

    summary_rows = []

    for cname, builder in CIRCUITS.items():
        print(f"{'='*70}")
        print(f"Building {cname}...")

        try:
            circuit = builder()
            print(f"  Built: {circuit.num_qubits}Q, {circuit.size()} gates")
        except Exception as e:
            print(f"  BUILD ERROR: {e}")
            continue

        # Pre-optimize (skip optimization stage)
        try:
            pm = generate_preset_pass_manager(2, backend=backend)
            pm.optimization = PassManager()
            pre_opt = pm.run(circuit)
        except Exception as e:
            print(f"  PRE-OPT ERROR: {e}")
            continue

        dag = circuit_to_dag(pre_opt)
        total_2q = sum(1 for node in dag.op_nodes() if len(node.qargs) == 2)

        # Collect 3Q blocks
        prop_set = PropertySet()
        collector = CollectMultiQBlocks(max_block_size=3)
        collector.property_set = prop_set
        collector.run(dag)
        blocks = prop_set.get("block_list", [])

        # Filter to 3Q blocks only
        blocks_3q = []
        for block in blocks:
            qubit_set = set()
            for node in block:
                for bit in node.qargs:
                    qubit_set.add(dag.find_bit(bit).index)
            if len(qubit_set) == 3:
                n_2q = sum(1 for node in block if len(node.qargs) == 2)
                blocks_3q.append((block, n_2q))

        n_blocks = len(blocks_3q)
        print(f"  Post-routing: {total_2q} 2Q gates, {n_blocks} 3Q blocks")

        if n_blocks == 0:
            print(f"  No 3Q blocks to analyze\n")
            summary_rows.append({
                "name": cname, "n_blocks": 0, "n_full_sep": 0,
                "n_bipartite": 0, "n_entangled": 0, "cx_savings": 0,
                "total_2q": total_2q,
            })
            continue

        # Analyze separability (sample if too many blocks)
        MAX_BLOCKS = 2000
        if n_blocks > MAX_BLOCKS:
            rng = np.random.default_rng(42)
            indices = rng.choice(n_blocks, MAX_BLOCKS, replace=False)
            sampled = [blocks_3q[i] for i in sorted(indices)]
            print(f"  Sampling {MAX_BLOCKS}/{n_blocks} blocks for analysis")
        else:
            sampled = blocks_3q

        t0 = time.perf_counter()
        n_full_sep = 0
        n_bipartite = 0
        n_entangled = 0
        cx_in_bipartite = []  # CX count of the 2Q part when bipartite
        original_2q_bipartite = []  # original 2Q gates in bipartite blocks
        cx_savings_total = 0
        errors = 0

        for block, orig_2q in sampled:
            try:
                U = block_to_unitary(block, dag)
                result = analyze_separability(U)

                if result["separable_type"] == "full":
                    n_full_sep += 1
                    cx_savings_total += orig_2q  # all 2Q gates can be removed
                elif result["separable_type"].startswith("bipartite"):
                    n_bipartite += 1
                    cx_after = result["cx_if_split"] if result["cx_if_split"] is not None else orig_2q
                    cx_in_bipartite.append(cx_after)
                    original_2q_bipartite.append(orig_2q)
                    saving = orig_2q - cx_after
                    if saving > 0:
                        cx_savings_total += saving
                else:
                    n_entangled += 1
            except Exception as e:
                errors += 1

        t_analysis = time.perf_counter() - t0
        n_analyzed = len(sampled)

        print(f"  Analysis time: {t_analysis*1000:.0f}ms ({t_analysis*1000/n_analyzed:.1f}ms/block)")
        print(f"  Fully separable (1Q⊗1Q⊗1Q): {n_full_sep}/{n_analyzed} "
              f"({100*n_full_sep/n_analyzed:.1f}%)")
        print(f"  Bipartite separable (2Q⊗1Q): {n_bipartite}/{n_analyzed} "
              f"({100*n_bipartite/n_analyzed:.1f}%)")
        print(f"  Entangled (not separable): {n_entangled}/{n_analyzed} "
              f"({100*n_entangled/n_analyzed:.1f}%)")
        if errors:
            print(f"  Errors: {errors}")

        if n_bipartite > 0:
            print(f"  Bipartite 2Q part CX count: "
                  f"avg={np.mean(cx_in_bipartite):.1f}, "
                  f"max={max(cx_in_bipartite)}, "
                  f"dist={dict(zip(*np.unique(cx_in_bipartite, return_counts=True)))}")
            print(f"  Original 2Q in bipartite blocks: "
                  f"avg={np.mean(original_2q_bipartite):.1f}")

        # Extrapolate savings if we sampled
        scale = n_blocks / n_analyzed if n_analyzed > 0 else 1
        cx_savings_est = int(cx_savings_total * scale)

        print(f"  Potential CX savings from splitting: {cx_savings_est}"
              f"{f' (extrapolated from {n_analyzed} samples)' if scale > 1 else ''}")
        if total_2q > 0:
            print(f"  As fraction of total 2Q gates: "
                  f"{100*cx_savings_est/total_2q:.2f}%")
        print()

        summary_rows.append({
            "name": cname,
            "n_blocks": n_blocks,
            "n_analyzed": n_analyzed,
            "n_full_sep": n_full_sep,
            "n_bipartite": n_bipartite,
            "n_entangled": n_entangled,
            "cx_savings": cx_savings_est,
            "total_2q": total_2q,
            "sep_pct": 100 * (n_full_sep + n_bipartite) / n_analyzed if n_analyzed > 0 else 0,
        })

    # Summary table
    print(f"\n{'='*70}")
    print("SUMMARY: 3Q Block Separability")
    print(f"{'='*70}\n")

    print(f"{'Circuit':>20s}  {'3Q Blks':>8s}  {'Full Sep':>9s}  {'Bipartite':>10s}  "
          f"{'Entangled':>10s}  {'CX Save':>8s}  {'Total 2Q':>9s}  {'Save %':>7s}")
    print("-" * 95)

    for r in summary_rows:
        pct = 100 * r["cx_savings"] / r["total_2q"] if r["total_2q"] > 0 else 0
        print(f"{r['name']:>20s}  {r['n_blocks']:8d}  {r['n_full_sep']:9d}  "
              f"{r['n_bipartite']:10d}  {r['n_entangled']:10d}  "
              f"{r['cx_savings']:8d}  {r['total_2q']:9d}  {pct:6.2f}%")

    print("-" * 95)
    totals = {k: sum(r[k] for r in summary_rows)
              for k in ["n_blocks", "n_full_sep", "n_bipartite", "n_entangled",
                        "cx_savings", "total_2q"]}
    pct = 100 * totals["cx_savings"] / totals["total_2q"] if totals["total_2q"] > 0 else 0
    print(f"{'TOTAL':>20s}  {totals['n_blocks']:8d}  {totals['n_full_sep']:9d}  "
          f"{totals['n_bipartite']:10d}  {totals['n_entangled']:10d}  "
          f"{totals['cx_savings']:8d}  {totals['total_2q']:9d}  {pct:6.2f}%")

    print(f"\nBottom line:")
    print(f"  Separable 3Q blocks: {totals['n_full_sep'] + totals['n_bipartite']} / "
          f"{totals['n_blocks']} "
          f"({100*(totals['n_full_sep']+totals['n_bipartite'])/totals['n_blocks']:.1f}%)"
          if totals['n_blocks'] > 0 else "  No 3Q blocks")
    print(f"  Potential CX savings: {totals['cx_savings']} / {totals['total_2q']} "
          f"({pct:.2f}%)")


if __name__ == "__main__":
    main()
