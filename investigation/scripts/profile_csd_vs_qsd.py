"""Compare CSD-based vs QSD-based 3Q synthesis on real circuit blocks.

Implements a Python CSD synthesizer (porting Pytket's ThreeQubitConversion algorithm)
and compares CX gate counts against Qiskit's QSD on 3Q blocks extracted from
standard + adversarial benchmark circuits.

The CSD algorithm (from Pytket's tket):
1. Separability check: if any qubit is separable (tensor product), decompose as 1Q + 2Q
2. CSD factorization: U = [L0, 0; 0, L1] · [C, -S; S, C] · [R0, 0; 0, R1]
3. Multiplexor synthesis: each [U0; U1] block-diagonal → Schur decomp → 2Q synthesis
4. Conjugation optimization: try 6 variants for each multiplexor, pick fewest CX

Reference: CQCL/tket — ThreeQubitConversion.cpp, CosSinDecomposition.cpp

Usage:
    cd ~/IBMWORK/QCSC/qiskit
    python investigation/profile_csd_vs_qsd.py
"""

import time
import warnings

import numpy as np
from scipy.linalg import schur

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
# CSD Linear Algebra (port of tket CosSinDecomposition.cpp)
# ===========================================================================

def cs_decomp(U):
    """Cosine-sine decomposition of a 2n×2n unitary.

    Returns (l0, l1, r0, r1, c, s) such that:
        U = [[l0, 0], [0, l1]] @ [[c, -s], [s, c]] @ [[r0, 0], [0, r1]]
    """
    N = U.shape[0]
    n = N // 2
    u00, u01 = U[:n, :n], U[:n, n:]
    u10, u11 = U[n:, :n], U[n:, n:]

    # SVD of u00, reversed for non-decreasing singular values
    L0, sigma, Vh = np.linalg.svd(u00)
    l0 = L0[:, ::-1]
    r0 = Vh[::-1, :]
    c_diag = sigma[::-1]
    r0_dag = r0.conj().T

    # QR of u10 @ r0*
    Q, R = np.linalg.qr(u10 @ r0_dag)
    l1 = Q

    # Make R diagonal, real, and non-negative
    s_diag = np.zeros(n)
    for j in range(n):
        z = R[j, j]
        r_abs = abs(z)
        if r_abs > 1e-12:
            l1[:, j] /= np.conj(z) / r_abs
            s_diag[j] = r_abs

    for j in range(n):
        if s_diag[j] < 0:
            s_diag[j] = -s_diag[j]
            l1[:, j] = -l1[:, j]

    # Compute r1
    r1 = np.zeros((n, n), dtype=complex)
    for i in range(n):
        if s_diag[i] > c_diag[i]:
            r1[i, :] = -(l0.conj().T @ u01)[i, :] / s_diag[i]
        else:
            r1[i, :] = (l1.conj().T @ u11)[i, :] / c_diag[i]

    return l0, l1, r0, r1, np.diag(c_diag), np.diag(s_diag)


# ===========================================================================
# 2Q Synthesis
# ===========================================================================

def count_cx_2q(U4):
    """CX count for optimal 2Q synthesis of a 4×4 unitary."""
    try:
        circ = two_qubit_cnot_decompose(U4)
        return circ.count_ops().get("cx", 0)
    except Exception:
        return 3


# ===========================================================================
# Separability Check (port of tket ThreeQubitConversion.cpp)
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


# Qubit permutation matrices
_P1 = np.eye(8)[[0, 1, 4, 5, 2, 3, 6, 7]]  # swap qubits 0,1
_P2 = np.eye(8)[[0, 4, 2, 6, 1, 5, 3, 7]]  # swap qubits 0,2


def check_separable(U):
    """Check all 3 bipartitions. Returns (partition, W_1q, V_2q) or None."""
    r = _separate_0_12(U)
    if r: return "0|12", r[0], r[1]
    r = _separate_0_12(_P1 @ U @ _P1.T)
    if r: return "1|02", r[0], r[1]
    r = _separate_0_12(_P2 @ U @ _P2.T)
    if r: return "2|01", r[0], r[1]
    return None


# ===========================================================================
# CSD 3Q Synthesis — Build Actual Circuit (port of tket three_qubit_synthesis)
# ===========================================================================

# Conjugation unitaries (tket get_conj_unitaries)
_CX01 = np.array([[1,0,0,0],[0,1,0,0],[0,0,0,1],[0,0,1,0]], dtype=complex)
_CX10 = np.array([[1,0,0,0],[0,0,0,1],[0,0,1,0],[0,1,0,0]], dtype=complex)
_SWAP = np.array([[1,0,0,0],[0,0,1,0],[0,1,0,0],[0,0,0,1]], dtype=complex)
CONJ_UNITARIES = [
    np.eye(4, dtype=complex),  # identity
    _SWAP,                      # SWAP
    _CX01,                      # CX(0,1)
    _CX10,                      # CX(1,0)
    _CX01 @ _CX10,             # CX(0,1)·CX(1,0)
    _CX10 @ _CX01,             # CX(1,0)·CX(0,1)
]

# CX cost of each conjugation (to subtract from synthesis cost)
CONJ_CX_COST = [0, 3, 1, 1, 2, 2]  # SWAP=3CX in standard decomp


def _diag_adjoint_plex_cx(D):
    """CX count for the diag-adjoint-plex [[D, 0], [0, D*]].

    Structure: Rz-CX-Rz-CX-Rz-CX-Rz-CX = 4 CX always.
    But if D ≈ scalar·I, the whole thing simplifies to 0 CX.
    """
    d = np.diag(D)
    # Check if all diagonal entries are the same phase
    if np.allclose(d, d[0] * np.ones(4), atol=1e-8):
        return 0
    # Check if only 2 distinct phases (can sometimes reduce)
    return 4


def _two_qubit_plex_build(U0, U1, extract_diagonal):
    """Build the multiplexor [[U0,0],[0,U1]] and return CX count.

    Port of tket two_qubit_plex. Tries 6 conjugation variants.
    The total structure is: R(2Q) + diag_plex(4CX) + L(2Q or 3Q)
    With extract_diagonal=True, L uses at most 2 CX.
    With extract_diagonal=False, L uses at most 3 CX.
    """
    U = U0 @ U1.conj().T
    T_mat, L = schur(U, output='complex')
    # T should be diagonal for unitary matrices
    D = np.zeros((4, 4), dtype=complex)
    for i in range(4):
        D[i, i] = np.sqrt(T_mat[i, i])
    R = D @ L.conj().T @ U1

    best_cx = None
    for idx, u_conj in enumerate(CONJ_UNITARIES):
        u_conj_adj = u_conj.conj().T

        # R part: conjugated R, synthesized optimally
        R_conj = u_conj @ R
        cx_R = count_cx_2q(R_conj)

        # Diagonal-adjoint-plex: conjugated D
        D_conj = u_conj @ D @ u_conj_adj
        cx_mid = _diag_adjoint_plex_cx(D_conj)

        # L part: conjugated L (with diagonal absorbed from R)
        # In Pytket, the diagonal from R's decompose_2cx_DV is absorbed into L.
        # For CX counting, this doesn't change the CX count of L itself.
        L_conj = L @ u_conj_adj
        cx_L = count_cx_2q(L_conj)

        # Conjugation costs: we wrap the subcircuit with u_conj and u_conj†
        # on qubits 1,2. These CX gates appear in the full circuit.
        # But in Pytket's design, the conjugation is absorbed into R and L,
        # so there's no extra cost — the conjugation IS the optimization.
        total = cx_R + cx_mid + cx_L
        if best_cx is None or total < best_cx:
            best_cx = total

    return best_cx


def _cossin_mid_cx(c, s):
    """CX count for the cos-sin middle block.

    Structure: Ry-H-CX-Ry-CX-Ry-CX-H-Ry = 3 CX.
    Trivial if s ≈ 0.
    """
    s_diag = np.diag(s)
    if np.allclose(s_diag, 0, atol=1e-8):
        return 0
    return 3


def csd_3q_cx_count(U):
    """CX count for CSD-based 3Q synthesis.

    Returns (cx_count, details).
    """
    # Step 1: separability
    sep = check_separable(U)
    if sep is not None:
        partition, W_1q, V_2q = sep
        cx_2q = count_cx_2q(V_2q)
        return cx_2q, {"method": "separable", "partition": partition}

    # Step 2: CSD
    l0, l1, r0, r1, c, s = cs_decomp(U)

    # Step 3: Right plex (with diagonal extraction)
    cx_right = _two_qubit_plex_build(r0, r1, extract_diagonal=True)

    # Step 4: Cos-sin middle
    cx_mid = _cossin_mid_cx(c, s)

    # Step 5: Left plex (adjust for sign flip, no diagonal extraction)
    l1_adj = l1.copy()
    l1_adj[:, 1] *= -1
    l1_adj[:, 3] *= -1
    cx_left = _two_qubit_plex_build(l0, l1_adj, extract_diagonal=False)

    total = cx_right + cx_mid + cx_left
    return total, {
        "method": "csd",
        "cx_right": cx_right,
        "cx_mid": cx_mid,
        "cx_left": cx_left,
    }


# ===========================================================================
# QSD-Based 3Q Synthesis via Qiskit
# ===========================================================================

def qsd_3q_cx_count(U):
    """CX count from Qiskit's QSD for an 8×8 unitary."""
    try:
        circ = qs_decomposition(U)
        ops = circ.count_ops()
        return ops.get("cx", 0) + ops.get("cz", 0)
    except Exception:
        return -1


# ===========================================================================
# Circuit Builders
# ===========================================================================

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


def block_to_unitary(block, dag):
    """Convert a block of DAGOpNodes to an 8×8 unitary matrix."""
    qubit_set = set()
    for node in block:
        for bit in node.qargs:
            qubit_set.add(dag.find_bit(bit).index)
    qubits = sorted(qubit_set)
    assert len(qubits) == 3

    qubit_map = {q: i for i, q in enumerate(qubits)}
    qc = QuantumCircuit(3)
    for node in block:
        gate_qubits = [qubit_map[dag.find_bit(bit).index] for bit in node.qargs]
        qc.append(node.op, gate_qubits)
    return Operator(qc).data


def count_block_2q(block):
    return sum(1 for node in block if len(node.qargs) == 2)


# ===========================================================================
# Main
# ===========================================================================

def main():
    backend = FakeTorino()
    print("=" * 70)
    print("CSD vs QSD 3Q Synthesis Comparison")
    print(f"Backend: FakeTorino ({backend.num_qubits} qubits)")
    print("=" * 70)

    summary_rows = []

    for cname, builder in CIRCUITS.items():
        print(f"\n{'='*60}")
        print(f"Circuit: {cname}")

        try:
            circuit = builder()
            print(f"  Built: {circuit.num_qubits}Q, {circuit.size()} gates")
        except Exception as e:
            print(f"  BUILD ERROR: {e}")
            continue

        # Pre-optimize (init + layout + routing + translation, no optimization)
        try:
            pm = generate_preset_pass_manager(2, backend=backend)
            pm.optimization = PassManager()
            pre_opt = pm.run(circuit)
        except Exception as e:
            print(f"  TRANSPILE ERROR: {e}")
            continue

        # Collect 3Q blocks
        dag = circuit_to_dag(pre_opt)
        ps = PropertySet()
        collector = CollectMultiQBlocks(max_block_size=3)
        collector.property_set = ps
        collector.run(dag)
        blocks = ps.get("block_list", [])

        # Filter to 3Q blocks
        blocks_3q = []
        for block in blocks:
            qubit_set = set()
            for node in block:
                for bit in node.qargs:
                    qubit_set.add(dag.find_bit(bit).index)
            if len(qubit_set) == 3:
                blocks_3q.append(block)

        print(f"  3Q blocks: {len(blocks_3q)}")
        if not blocks_3q:
            continue

        # Sample (up to 200)
        rng = np.random.default_rng(42)
        if len(blocks_3q) > 200:
            idx = rng.choice(len(blocks_3q), 200, replace=False)
            sampled = [blocks_3q[i] for i in sorted(idx)]
            print(f"  Sampled 200 blocks")
        else:
            sampled = blocks_3q

        # Compare
        results = []
        n_sep = 0
        n_csd_wins = 0
        n_qsd_wins = 0
        n_tie = 0
        n_error = 0
        t0 = time.perf_counter()

        for block in sampled:
            orig_cx = count_block_2q(block)
            try:
                U = block_to_unitary(block, dag)
            except Exception:
                n_error += 1
                continue

            try:
                cx_csd, details = csd_3q_cx_count(U)
            except Exception:
                cx_csd = -1
                details = {"method": "error"}

            cx_qsd = qsd_3q_cx_count(U)

            if cx_csd < 0 or cx_qsd < 0:
                n_error += 1
                continue

            if details.get("method") == "separable":
                n_sep += 1

            if cx_csd < cx_qsd:
                n_csd_wins += 1
            elif cx_qsd < cx_csd:
                n_qsd_wins += 1
            else:
                n_tie += 1

            results.append({
                "orig": orig_cx,
                "csd": cx_csd,
                "qsd": cx_qsd,
                "method": details.get("method"),
            })

        dt = time.perf_counter() - t0
        n = len(results)
        if not n:
            print("  No valid comparisons")
            continue

        orig = [r["orig"] for r in results]
        csd = [r["csd"] for r in results]
        qsd = [r["qsd"] for r in results]

        csd_helps = sum(1 for r in results if r["csd"] < r["orig"])
        qsd_helps = sum(1 for r in results if r["qsd"] < r["orig"])

        # Gate guard effectiveness: how many blocks would each method replace?
        csd_guard = sum(1 for r in results if r["csd"] < r["orig"])
        qsd_guard = sum(1 for r in results if r["qsd"] < r["orig"])

        # Total CX savings if gate guard is applied
        csd_savings = sum(r["orig"] - r["csd"] for r in results if r["csd"] < r["orig"])
        qsd_savings = sum(r["orig"] - r["qsd"] for r in results if r["qsd"] < r["orig"])
        total_orig = sum(orig)

        print(f"  Time: {dt:.1f}s ({dt*1000/n:.1f}ms/block)")
        print(f"  Separable: {n_sep}/{n} ({100*n_sep/n:.1f}%)")
        print(f"  Errors: {n_error}")
        print()
        print(f"  {'':>15} {'Median':>7} {'Mean':>7} {'Max':>5} {'Total':>7}")
        print(f"  {'Original CX':>15} {np.median(orig):>7.0f} {np.mean(orig):>7.1f} "
              f"{max(orig):>5} {sum(orig):>7}")
        print(f"  {'CSD CX':>15} {np.median(csd):>7.0f} {np.mean(csd):>7.1f} "
              f"{max(csd):>5} {sum(csd):>7}")
        print(f"  {'QSD CX':>15} {np.median(qsd):>7.0f} {np.mean(qsd):>7.1f} "
              f"{max(qsd):>5} {sum(qsd):>7}")
        print()
        print(f"  Head-to-head: CSD wins {n_csd_wins} | QSD wins {n_qsd_wins} | Tie {n_tie}")
        print(f"  With gate guard:")
        print(f"    CSD replaces {csd_guard}/{n} blocks, saves {csd_savings} CX "
              f"({100*csd_savings/total_orig:.1f}% of total)")
        print(f"    QSD replaces {qsd_guard}/{n} blocks, saves {qsd_savings} CX "
              f"({100*qsd_savings/total_orig:.1f}% of total)")

        # CSD CX distribution
        csd_dist = {}
        for r in results:
            csd_dist[r["csd"]] = csd_dist.get(r["csd"], 0) + 1
        print(f"\n  CSD CX distribution: {dict(sorted(csd_dist.items()))}")

        summary_rows.append({
            "circuit": cname,
            "n": n,
            "med_orig": np.median(orig),
            "med_csd": np.median(csd),
            "med_qsd": np.median(qsd),
            "csd_wins": n_csd_wins,
            "qsd_wins": n_qsd_wins,
            "sep": n_sep,
            "csd_saves": csd_savings,
            "qsd_saves": qsd_savings,
            "total_orig": total_orig,
        })

    # Summary
    print(f"\n\n{'='*90}")
    print("SUMMARY: CSD vs QSD with Gate Guard")
    print(f"{'='*90}")
    print(f"{'Circuit':<20} {'N':>4} {'Med':>4} {'CSD':>4} {'QSD':>4} "
          f"{'CSD>QSD':>7} {'QSD>CSD':>7} {'Sep':>4} "
          f"{'CSD save':>8} {'QSD save':>8} {'Orig':>6}")
    print("-" * 90)
    for r in summary_rows:
        print(f"{r['circuit']:<20} {r['n']:>4} {r['med_orig']:>4.0f} "
              f"{r['med_csd']:>4.0f} {r['med_qsd']:>4.0f} "
              f"{r['csd_wins']:>7} {r['qsd_wins']:>7} {r['sep']:>4} "
              f"{r['csd_saves']:>8} {r['qsd_saves']:>8} {r['total_orig']:>6}")

    total_csd = sum(r["csd_saves"] for r in summary_rows)
    total_qsd = sum(r["qsd_saves"] for r in summary_rows)
    total_all = sum(r["total_orig"] for r in summary_rows)
    print("-" * 90)
    print(f"{'TOTAL':<20} {'':>4} {'':>4} {'':>4} {'':>4} "
          f"{'':>7} {'':>7} {'':>4} "
          f"{total_csd:>8} {total_qsd:>8} {total_all:>6}")
    if total_all > 0:
        print(f"\n  Overall CX savings: CSD {100*total_csd/total_all:.1f}% | "
              f"QSD {100*total_qsd/total_all:.1f}%")


if __name__ == "__main__":
    main()
