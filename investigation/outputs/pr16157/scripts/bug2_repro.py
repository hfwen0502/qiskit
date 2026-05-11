"""Reproducer for a Qiskit L2 transpiler non-determinism we observed during
PR #16157 evidence gathering.

Even with `seed_transpiler` passed explicitly, two circuits produce
non-deterministic output across fresh Python subprocesses on Qiskit main.
The 2Q gate count stays identical across runs; a small number of 1Q gates
shift between equivalent rotational decompositions. Suggests a tie-break
in a 1Q rotation pass that consumes process-dependent state (e.g., ahash
iteration order, unseeded RandomState).

We surveyed ~1000 benchpress circuits and found exactly two affected:
  1. ham_mu_z_prime_enc_gray_dvalues_4-4-4-4  (4Q hamlib; FakeTorino)
  2. ham_ham_JW24 on all-to-all 24Q topology   (24Q JW hamiltonian)

Both produce identical 2Q counts across runs; only 1Q decomposition drifts.
Behavior is deterministic in the same process (repeated pm.run calls) but
varies between subprocesses.

Usage:
    ~/.venv-main/bin/python bug2_repro.py [N]

where N is the number of fresh subprocesses to run per circuit (default 20).
If all N subprocesses produce the same output, the bug did not trigger in
this sample; increase N. In our measurements the distribution is roughly
60-80% / 20-40% across two distinct outputs.

The two observed outputs from PR #16157's full benchpress run
(seed_transpiler=1, FakeTorino / FlexibleBackend with seed=12345):

  ham_mu_z_prime_enc_gray_dvalues_4-4-4-4 (observed rate ~20-40% per call):
      A) {rz: 17, sx: 16, cz: 4}             total=37  depth=11
      B) {rz: 16, sx: 16, cz: 4, x: 2}       total=38  depth=14

  ham_ham_JW24 on all-to-all 24Q (observed rate ~0.25% per call, 1/400 runs):
      A) {rz: 59345, sx: 58910, cz: 46079, x: 5552}  depth=141636
      B) {rz: 59350, sx: 58914, cz: 46079, x: 5550}  depth~same

  JW24 transpile is ~20s and only one bucket appears in ~10-20 standalone
  runs; to reproduce both buckets reliably, either run ~300+ subprocesses
  or run the full benchpress abstract_hamiltonians suite twice and diff
  the two main.json files (the one flaky circuit will differ).

Qiskit: tested against main (c25216340) and parallel-optimization-passes
base (03c640f73). Both affected.
"""
import sys
import subprocess
import json

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20

HAMLIB_JSON = "/Users/hfwen/IBMWORK/QCSC/benchpress/benchpress/hamiltonian/hamlib/100_representative.json"
SEED = 1   # any non-None seed; both circuits reproduce the bug with the same seed


def find_record(substring):
    with open(HAMLIB_JSON) as f:
        records = json.load(f)
    for r in records:
        if substring in r.get("ham_instance", ""):
            return r
    raise RuntimeError(f"not found: {substring}")


CHILD_FAKETORINO = '''
import json, sys
from qiskit import QuantumCircuit
from qiskit.quantum_info import SparsePauliOp
from qiskit.circuit.library import PauliEvolutionGate
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeTorino

with open(sys.argv[1]) as f:
    terms, coefs = json.load(f)
seed = int(sys.argv[2])
op = SparsePauliOp(terms, coefs)
qc = QuantumCircuit(op.num_qubits)
qc.append(PauliEvolutionGate(op, time=1), qargs=range(op.num_qubits))
pm = generate_preset_pass_manager(2, FakeTorino(), seed_transpiler=seed)
trans = pm.run(qc)
print(json.dumps({"ops": dict(trans.count_ops()), "depth": trans.depth()}))
'''

CHILD_FLEXIBLE = '''
import json, sys
from qiskit import QuantumCircuit
from qiskit.quantum_info import SparsePauliOp
from qiskit.circuit.library import PauliEvolutionGate
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.providers.fake_provider import GenericBackendV2
from qiskit.transpiler import CouplingMap

with open(sys.argv[1]) as f:
    terms, coefs = json.load(f)
seed = int(sys.argv[2])
op = SparsePauliOp(terms, coefs)
qc = QuantumCircuit(op.num_qubits)
qc.append(PauliEvolutionGate(op, time=1), qargs=range(op.num_qubits))

# Matches benchpress FlexibleBackend(n, "all-to-all", control_flow=True) with
# the seed=12345 fix applied (benchpress_patch.diff in this dir).
cmap = CouplingMap.from_full(op.num_qubits)
cmap.make_symmetric()
backend = GenericBackendV2(
    op.num_qubits,
    basis_gates=["id", "sx", "x", "rz", "cz"],
    coupling_map=cmap,
    control_flow=True,
    seed=12345,
)
pm = generate_preset_pass_manager(optimization_level=2, backend=backend, seed_transpiler=seed)
trans = pm.run(qc)
print(json.dumps({"ops": dict(trans.count_ops()), "depth": trans.depth()}))
'''


def run_case(label, child_code, terms, coefs):
    import tempfile, os
    fd, payload = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(payload, "w") as f:
        json.dump([terms, coefs], f)
    try:
        print(f"--- {label} ---")
        outputs = {}
        for i in range(1, N + 1):
            out = subprocess.check_output(
                [sys.executable, "-c", child_code, payload, str(SEED)],
                text=True,
                timeout=600,
            ).strip()
            outputs[out] = outputs.get(out, 0) + 1
            if i <= 5 or i == N:
                print(f"  run {i:2d}: {out}")
        print(f"  {N} subprocesses, seed_transpiler={SEED}")
        print(f"  Distinct outputs: {len(outputs)}")
        for k, c in sorted(outputs.items(), key=lambda x: -x[1]):
            print(f"    {c:3d}x  {k}")
        print()
        return outputs
    finally:
        os.unlink(payload)


rec_muz = find_record("mu_z_prime_enc_gray_dvalues_4-4-4-4")
run_case(
    "ham_mu_z_prime_enc_gray_dvalues_4-4-4-4 on FakeTorino (L2, seed=1)",
    CHILD_FAKETORINO,
    rec_muz["ham_hamlib_hamiltonian_terms"],
    rec_muz["ham_hamlib_hamiltonian_coefficients"],
)

rec_jw = find_record("/ham_JW24,")
run_case(
    "ham_ham_JW24 on all-to-all 24Q GenericBackendV2(seed=12345) (L2, seed=1)",
    CHILD_FLEXIBLE,
    rec_jw["ham_hamlib_hamiltonian_terms"],
    rec_jw["ham_hamlib_hamiltonian_coefficients"],
)

print("Expected: 2 distinct outputs per circuit. If a single output is")
print("observed for one circuit, increase N — the bug is probabilistic.")
