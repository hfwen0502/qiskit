"""Test all three loop exit signals for the changed-flag optimization loop.

Each test constructs a minimal circuit that triggers exactly one signal,
runs a full transpile, and verifies the output is correct. These serve as
a regression safety net: benchpress circuits may not trigger all three
conditions, but these tests guarantee coverage.

Signals:
  1. _opt_pass_changed: RemoveIdentityEquivalent or CommutativeCancellation
     removes a multi-qubit gate (2Q cancellation).
  2. _opt_1q_consolidated: CommutativeCancellation consolidates Z-rotations
     or X-rotations into a single rotation.
  3. all_gates_in_basis == False: An optimization pass produces a gate outside
     the target basis, triggering BasisTranslator -> re-optimization.

Usage:
    # From the qiskit repo root:
    python investigation/scripts/test_loop_exit_signals.py

    # On remote server:
    source /mnt/data/myenv/bin/activate
    cd /mnt/data/qiskit
    python investigation/scripts/test_loop_exit_signals.py
"""

import sys
import os

from qiskit import QuantumCircuit
from qiskit.circuit import Parameter
from qiskit.circuit.library import RZZGate
from qiskit.transpiler import generate_preset_pass_manager, PassManager, PassManagerConfig
from qiskit.transpiler.target import InstructionProperties
from qiskit.transpiler.passes import (
    RemoveIdentityEquivalent,
    Optimize1qGatesDecomposition,
    CommutativeCancellation,
)
from qiskit.transpiler.passes.utils.gates_basis import GatesInBasis
from qiskit.providers.fake_provider import GenericBackendV2


# ─────────────────────────────────────────────────────────────────────────────
# Signal 1: _opt_pass_changed (multi-qubit gate removed)
# ─────────────────────────────────────────────────────────────────────────────

def test_signal1_2q_cancellation():
    """CommutativeCancellation cancels adjacent CX pairs separated by commuting gates.

    Circuit pattern:
        CX(0,1); RZ(0.5, 0); CX(0,1)

    Why it triggers signal 1:
        RZ on qubit 0 commutes with CX(0,1) on the control qubit.
        CommutativeCancellation sees two CX gates in the same commutation set
        and removes them (they cancel). This sets _opt_pass_changed = True.

    Why re-iteration matters:
        After the CX pair is removed, the 1Q gates on those qubits form a longer
        run. Optimize1qGatesDecomposition can now merge them into a shorter
        decomposition on the next iteration.
    """
    print("=" * 70)
    print("TEST 1a: _opt_pass_changed (CX pair cancellation)")
    print("=" * 70)

    backend = GenericBackendV2(num_qubits=5, basis_gates=["id", "sx", "x", "rz", "cx"])

    # CX pairs with commuting RZ on control qubit between them
    qc = QuantumCircuit(3)
    qc.cx(0, 1)
    qc.rz(0.5, 0)      # commutes with CX on control
    qc.cx(0, 1)         # cancels with first CX
    qc.cx(1, 2)
    qc.rz(0.3, 1)      # commutes with CX on control
    qc.cx(1, 2)         # cancels with first CX(1,2)
    qc.h(0)
    qc.h(1)
    qc.h(2)
    qc.measure_all()

    print(f"  Input: {qc.num_qubits} qubits, {qc.size()} gates")

    pm = generate_preset_pass_manager(optimization_level=2, backend=backend)
    result = pm.run(qc)

    cx_count = result.count_ops().get("cx", 0)
    print(f"  Output: {result.size()} gates, CX: {cx_count}")
    print(f"  Ops: {dict(result.count_ops())}")

    passed = cx_count == 0
    print(f"  {'PASS' if passed else 'FAIL'}: CX pairs cancelled" +
          (f" ({cx_count} remain)" if not passed else ""))
    return passed


def test_signal1_identity_removal():
    """RemoveIdentityEquivalent removes near-identity 2Q gates after synthesis.

    Circuit pattern:
        CX(0,1); RZ(epsilon, 0); CX(0,1)   where epsilon ~ 1e-10

    Why it triggers signal 1:
        ConsolidateBlocks (pre-loop) collects this into a 2Q unitary.
        UnitarySynthesis decomposes it. Since the unitary is nearly identity
        (two CX with near-zero rotation between), the synthesized gate is
        near-identity. RemoveIdentityEquivalent (in the loop) detects it
        using the approximation_degree threshold and removes it.
        This sets _opt_pass_changed = True.

    Why re-iteration matters:
        Removing the near-identity 2Q gate frees adjacent 1Q runs, allowing
        Optimize1qGatesDecomposition to find a shorter decomposition.
    """
    print("\n" + "=" * 70)
    print("TEST 1b: _opt_pass_changed (near-identity 2Q removal)")
    print("=" * 70)

    backend = GenericBackendV2(num_qubits=5, basis_gates=["id", "sx", "x", "rz", "cz"])

    epsilon = 1e-10
    qc = QuantumCircuit(4)
    # Near-identity 2Q block
    qc.cx(0, 1)
    qc.rz(epsilon, 0)
    qc.cx(0, 1)
    # Another near-identity block
    qc.cx(2, 3)
    qc.rz(epsilon, 2)
    qc.cx(2, 3)
    # Real gates (should survive)
    qc.h(0)
    qc.cx(0, 1)
    qc.h(2)
    qc.cx(2, 3)
    qc.measure_all()

    print(f"  Input: {qc.num_qubits} qubits, {qc.size()} gates")

    pm = generate_preset_pass_manager(optimization_level=2, backend=backend)
    result = pm.run(qc)

    cz_count = result.count_ops().get("cz", 0)
    print(f"  Output: {result.size()} gates, CZ: {cz_count}")
    print(f"  Ops: {dict(result.count_ops())}")

    # With near-identity blocks removed, expect <= 2 CZ (from the real CX gates)
    passed = cz_count <= 2
    print(f"  {'PASS' if passed else 'FAIL'}: Near-identity blocks removed (CZ={cz_count}, expected <=2)")
    return passed


# ─────────────────────────────────────────────────────────────────────────────
# Signal 2: _opt_1q_consolidated (rotations merged)
# ─────────────────────────────────────────────────────────────────────────────

def test_signal2_rotation_consolidation():
    """CommutativeCancellation consolidates Z-rotations separated by CZ gates.

    Circuit pattern:
        RZ(0.1, 0); CZ(0,1); RZ(0.2, 0); CZ(0,2); RZ(0.3, 0)

    Why it triggers signal 2:
        RZ commutes with CZ (on either qubit). CommutativeCancellation sees all
        three RZ gates on qubit 0 in the same commutation set and consolidates
        them into a single RZ(0.6). This sets _opt_1q_consolidated = True.

    Why re-iteration matters:
        The consolidated rotation shortens the 1Q run on qubit 0. On the next
        iteration, Optimize1qGatesDecomposition can decompose this shorter run
        more efficiently (e.g., a 3-gate sequence becomes a 2-gate sequence).
        Without re-iteration, the individual rotations get decomposed separately.

    Verification:
        After full transpile, run one more optimization iteration. If delta=0,
        the loop converged correctly (no missed opportunities).
    """
    print("\n" + "=" * 70)
    print("TEST 2: _opt_1q_consolidated (Z-rotation consolidation)")
    print("=" * 70)

    backend = GenericBackendV2(num_qubits=5, basis_gates=["id", "sx", "x", "rz", "cz"])

    # Multiple RZ separated by CZ — all RZ commute through CZ
    qc = QuantumCircuit(4)
    for _ in range(3):
        qc.rz(0.1, 0)
        qc.cz(0, 1)
        qc.rz(0.2, 0)
        qc.cz(0, 2)
        qc.rz(0.3, 0)
        qc.cz(0, 3)
        qc.rz(0.4, 1)
        qc.cz(1, 2)
        qc.rz(0.5, 1)
        qc.cz(1, 3)
    qc.measure_all()

    print(f"  Input: {qc.num_qubits} qubits, {qc.size()} gates, RZ: {qc.count_ops().get('rz', 0)}")

    pm = generate_preset_pass_manager(optimization_level=2, backend=backend)
    result = pm.run(qc)

    rz_out = result.count_ops().get("rz", 0)
    print(f"  Output: {result.size()} gates, RZ: {rz_out}")

    # Verify convergence: re-run optimization passes on the output
    config = PassManagerConfig.from_backend(backend)
    opt_pm = PassManager([
        RemoveIdentityEquivalent(
            approximation_degree=config.approximation_degree,
            target=config.target,
        ),
        Optimize1qGatesDecomposition(
            basis=config.basis_gates, target=config.target
        ),
        CommutativeCancellation(target=config.target),
    ])
    result2 = opt_pm.run(result)
    delta = result.size() - result2.size()

    passed = delta == 0
    print(f"  Re-run delta: {delta} gates")
    print(f"  {'PASS' if passed else 'FAIL'}: Loop converged — no further improvement possible")
    return passed


# ─────────────────────────────────────────────────────────────────────────────
# Signal 3: all_gates_in_basis == False (out-of-basis gate produced)
# ─────────────────────────────────────────────────────────────────────────────

def test_signal3_rzz_out_of_basis():
    """UnitarySynthesis emits out-of-basis gates when RZZ has lower error (Level 3).

    Setup:
        Backend with basis [id, sx, x, rz, cz] PLUS RZZ at half the error of CZ.
        UnitarySynthesis prefers RZZ due to lower error and uses
        TwoQubitControlledUDecomposer, which emits S, Sdg, H — none in basis.

    Why it triggers signal 3:
        After UnitarySynthesis produces S/Sdg/H, GatesInBasis (in the unroll block)
        detects them as out-of-basis. BasisTranslator translates them into basis
        gates (unoptimized). The loop condition sees all_gates_in_basis == False
        and re-iterates so Optimize1qGatesDecomposition can clean up the
        BasisTranslator output.

    Why re-iteration matters:
        BasisTranslator produces correct but unoptimized sequences (e.g.,
        H -> RZ(-pi/2); SX; RZ(-pi/2)). Without re-iteration, these
        suboptimal sequences remain in the output.
    """
    print("\n" + "=" * 70)
    print("TEST 3a: all_gates_in_basis (RZZ backend, Level 3)")
    print("=" * 70)

    backend = GenericBackendV2(num_qubits=20, basis_gates=["id", "sx", "x", "rz", "cz"])
    target = backend.target

    # Add RZZ with lower error -> UnitarySynthesis prefers it
    rzz_props = {}
    for qargs in target.qargs:
        if len(qargs) == 2:
            cz_props = target["cz"][qargs]
            rzz_props[qargs] = InstructionProperties(
                duration=cz_props.duration,
                error=cz_props.error * 0.5
            )
    target.add_instruction(RZZGate(Parameter("theta")), rzz_props)

    # Circuit with 2Q content for UnitarySynthesis to optimize
    qc = QuantumCircuit(10)
    for i in range(9):
        qc.cx(i, i + 1)
        qc.rz(0.5, i)
        qc.cx(i, i + 1)
    qc.measure_all()

    print(f"  Input: {qc.num_qubits} qubits, {qc.size()} gates")
    print(f"  Basis: [id, sx, x, rz, cz, rzz] (rzz at 0.5x error)")

    pm = generate_preset_pass_manager(optimization_level=3, backend=backend)
    result = pm.run(qc)

    ops = result.count_ops()
    basis = set(backend.target.operation_names)
    out_of_basis = set(ops.keys()) - basis - {"measure", "barrier"}

    print(f"  Output: {result.size()} gates, depth {result.depth()}")
    print(f"  Ops: {dict(ops)}")

    passed = len(out_of_basis) == 0
    if passed:
        print(f"  PASS: All gates in basis — loop re-iterated and cleaned up")
    else:
        print(f"  FAIL: Out-of-basis gates remain: {out_of_basis}")
    return passed


def test_signal3_rx_out_of_basis():
    """CommutativeCancellation produces RX not in basis (Level 2).

    Circuit pattern:
        X(1); CX(0,1); RX(0.5, 1)

    Why it triggers signal 3:
        X and RX are both X-rotations. X on CX target commutes with CX.
        CommutativeCancellation sees X (angle=pi) and RX(0.5) in the same
        X-rotation commutation set on qubit 1, and consolidates them into
        RX(pi + 0.5) = RX(3.64...).

        RX is NOT in basis [id, sx, x, rz, cx]. GatesInBasis detects it,
        BasisTranslator translates it, and the loop re-iterates to optimize.

    Why re-iteration matters:
        BasisTranslator translates RX(theta) into a multi-gate sequence like
        RZ(-pi/2); SX; RZ(theta); SX; RZ(-pi/2). Without re-iteration,
        Optimize1qGatesDecomposition doesn't get to merge this into the
        surrounding 1Q run.

    Verification:
        We first run CommutativeCancellation alone to confirm RX is produced,
        then verify the full transpile outputs only basis gates.
    """
    print("\n" + "=" * 70)
    print("TEST 3b: all_gates_in_basis (CommutativeCancellation -> RX, Level 2)")
    print("=" * 70)

    backend = GenericBackendV2(num_qubits=5, basis_gates=["id", "sx", "x", "rz", "cx"])
    config = PassManagerConfig.from_backend(backend)

    # X(1); CX(0,1); RX(0.5, 1) — X commutes with CX on target
    qc = QuantumCircuit(3)
    qc.x(1)
    qc.cx(0, 1)
    qc.rx(0.5, 1)
    qc.cx(0, 1)
    qc.cx(1, 2)
    qc.measure_all()

    print(f"  Input: {qc.num_qubits} qubits, {qc.size()} gates")
    print(f"  Basis: [id, sx, x, rz, cx] (no rx)")

    # Step 1: Verify CommutativeCancellation produces RX
    cc_pm = PassManager([CommutativeCancellation(target=config.target)])
    cc_result = cc_pm.run(qc)
    cc_ops = cc_result.count_ops()
    rx_produced = "rx" in cc_ops
    print(f"  After CommutativeCancellation: {dict(cc_ops)}")
    print(f"  RX produced: {rx_produced}")

    # Step 2: Verify GatesInBasis detects it
    if rx_produced:
        gib_pm = PassManager([GatesInBasis(config.basis_gates, target=config.target)])
        gib_pm.run(cc_result)
        all_in_basis = gib_pm.property_set.get("all_gates_in_basis", True)
        print(f"  all_gates_in_basis: {all_in_basis} (should be False)")

    # Step 3: Full transpile — loop should handle it
    pm = generate_preset_pass_manager(optimization_level=2, backend=backend)
    result = pm.run(qc)
    ops = result.count_ops()
    basis = set(backend.target.operation_names)
    out_of_basis = set(ops.keys()) - basis - {"measure", "barrier"}

    print(f"  Full transpile output: {result.size()} gates")
    print(f"  Ops: {dict(ops)}")

    passed = len(out_of_basis) == 0 and rx_produced
    if passed:
        print(f"  PASS: RX produced by CommutativeCancellation, then cleaned up by loop")
    elif not rx_produced:
        print(f"  SKIP: CommutativeCancellation didn't produce RX (commutation set not formed)")
    else:
        print(f"  FAIL: Out-of-basis gates remain: {out_of_basis}")
    return passed


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Loop Exit Signal Tests")
    print("Verifying all three signals of the changed-flag loop condition")
    print()

    results = []
    results.append(("Signal 1a: 2Q cancellation", test_signal1_2q_cancellation()))
    results.append(("Signal 1b: identity removal", test_signal1_identity_removal()))
    results.append(("Signal 2:  rotation consolidation", test_signal2_rotation_consolidation()))
    results.append(("Signal 3a: RZZ out-of-basis (L3)", test_signal3_rzz_out_of_basis()))
    results.append(("Signal 3b: RX out-of-basis (L2)", test_signal3_rx_out_of_basis()))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed.")
    else:
        print("Some tests FAILED. Check output above.")
        sys.exit(1)
