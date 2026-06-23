"""Count Level-2 optimization-loop iterations per circuit (one execution of
CommutativeCancellation == one loop iteration). Reuses benchpress's own circuit
+ backend construction so all six groups are covered. Prints TSV: group<TAB>name<TAB>iters.
Per-circuit errors are caught and emitted as ERR rows so they can't abort the sweep.
"""
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, "/mnt/data/spotter-val/benchpress")

from qiskit import QuantumCircuit
from qiskit.quantum_info import SparsePauliOp
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime.fake_provider import FakeNighthawk

from benchpress.config import Configuration
from benchpress.utilities.backends import FlexibleBackend
from benchpress.utilities.io.hamiltonians import generate_hamiltonian_circuit
from benchpress.workouts.abstract_transpile.qasmbench import (
    SMALL_CIRC_TOPO, SMALL_NAMES, MEDIUM_CIRC_TOPO, MEDIUM_NAMES,
    LARGE_CIRC_TOPO, LARGE_NAMES)
from benchpress.workouts.abstract_transpile.hamlib_hamiltonians import HAM_TOPO, HAM_TOPO_NAMES

LOOP_PASS = "CommutativeCancellation"
NH = FakeNighthawk()


class _B:
    def __init__(self):
        self.extra_info = {}


def n_iters(backend, circ):
    pm = generate_preset_pass_manager(2, backend, seed_transpiler=1)
    n = [0]
    pm.run(circ, callback=lambda **kw: n.__setitem__(
        0, n[0] + (type(kw["pass_"]).__name__ == LOOP_PASS)))
    return n[0]


def run_one(group, name, mk):
    """mk() returns (backend, circuit)."""
    try:
        backend, circ = mk()
        if circ.num_qubits > backend.num_qubits:
            return
        print(f"{group}\t{name}\t{n_iters(backend, circ)}", flush=True)
    except Exception as e:
        print(f"{group}\t{name}\tERR:{type(e).__name__}", flush=True)


fdir = "/mnt/data/spotter-val/benchpress/benchpress/qasm/feynman"
for f in sorted(os.listdir(fdir)):
    if f.endswith(".qasm"):
        run_one("feynman", f[:-5],
                lambda f=f: (NH, QuantumCircuit.from_qasm_file(f"{fdir}/{f}")))

hdir = Configuration.get_hamiltonian_dir("hamlib")
for h in json.load(open(hdir + "100_representative.json")):
    if h["ham_qubits"] > NH.num_qubits:
        continue
    run_one("device_hamiltonians", "ham_" + h["ham_instance"][1:-1],
            lambda h=h: (NH, generate_hamiltonian_circuit(
                SparsePauliOp(h["ham_hamlib_hamiltonian_terms"],
                              h["ham_hamlib_hamiltonian_coefficients"]), _B())))

for names, ct, grp in [(SMALL_NAMES, SMALL_CIRC_TOPO, "abstract_small"),
                       (MEDIUM_NAMES, MEDIUM_CIRC_TOPO, "abstract_medium"),
                       (LARGE_NAMES, LARGE_CIRC_TOPO, "abstract_large")]:
    for nm, (qasm, topo) in zip(names, ct):
        run_one(grp, nm, lambda qasm=qasm, topo=topo: (
            lambda c: (FlexibleBackend(c.num_qubits, topo, control_flow=True), c))(
                QuantumCircuit.from_qasm_file(qasm)))

for (ham, topo), nm in zip(HAM_TOPO, HAM_TOPO_NAMES):
    run_one("abstract_hamiltonians", nm, lambda ham=ham, topo=topo: (
        lambda c: (FlexibleBackend(c.num_qubits, topo, control_flow=True), c))(
            generate_hamiltonian_circuit(ham["ham_hamlib_hamiltonian"], _B())))
