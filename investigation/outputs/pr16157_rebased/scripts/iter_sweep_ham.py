"""Iteration counts for the two Hamiltonian groups. Calls qiskit_hamiltonian_circuit
directly (bypasses the gym_name dispatch that failed in the standalone run).
TSV: group<TAB>name<TAB>iters."""
import json
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/mnt/data/spotter-val/benchpress")
from qiskit.quantum_info import SparsePauliOp
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeNighthawk
from benchpress.config import Configuration
from benchpress.utilities.backends import FlexibleBackend
from benchpress.qiskit_gym.utils.io import qiskit_hamiltonian_circuit
from benchpress.workouts.abstract_transpile.hamlib_hamiltonians import HAM_TOPO, HAM_TOPO_NAMES

LOOP_PASS = "CommutativeCancellation"
NH = FakeNighthawk()


def n_iters(be, circ):
    pm = generate_preset_pass_manager(2, be, seed_transpiler=1)
    n = [0]
    pm.run(circ, callback=lambda **kw: n.__setitem__(
        0, n[0] + (type(kw["pass_"]).__name__ == LOOP_PASS)))
    return n[0]


def run_one(g, name, mk):
    try:
        be, c = mk()
        if c.num_qubits > be.num_qubits:
            return
        print(f"{g}\t{name}\t{n_iters(be, c)}", flush=True)
    except Exception as e:
        print(f"{g}\t{name}\tERR:{type(e).__name__}", flush=True)


hdir = Configuration.get_hamiltonian_dir("hamlib")
for h in json.load(open(hdir + "100_representative.json")):
    if h["ham_qubits"] > NH.num_qubits:
        continue
    run_one("device_hamiltonians", "ham_" + h["ham_instance"][1:-1],
            lambda h=h: (NH, qiskit_hamiltonian_circuit(SparsePauliOp(
                h["ham_hamlib_hamiltonian_terms"], h["ham_hamlib_hamiltonian_coefficients"]))))

for (ham, topo), nm in zip(HAM_TOPO, HAM_TOPO_NAMES):
    run_one("abstract_hamiltonians", nm, lambda ham=ham, topo=topo: (
        lambda c: (FlexibleBackend(c.num_qubits, topo, control_flow=True), c))(
            qiskit_hamiltonian_circuit(ham["ham_hamlib_hamiltonian"])))
