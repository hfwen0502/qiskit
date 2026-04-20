// This code is part of Qiskit.
//
// (C) Copyright IBM 2025
//
// This code is licensed under the Apache License, Version 2.0. You may
// obtain a copy of this license in the LICENSE.txt file in the root directory
// of this source tree or at https://www.apache.org/licenses/LICENSE-2.0.
//
// Any modifications or derivative works of this code must retain this
// copyright notice, and modified files need to carry a notice indicating
// that they have been altered from the originals.

use hashbrown::HashSet;
use num_complex::Complex64;

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use ndarray::prelude::*;
use rayon::prelude::*;
use rustworkx_core::petgraph::stable_graph::NodeIndex;

use qiskit_circuit::dag_circuit::{DAGCircuit, NodeType};
use qiskit_circuit::operations::{Operation, OperationRef, Param};

use crate::target::{Target, TargetOperation};
use qiskit_circuit::instruction::Instruction;
use qiskit_circuit::{PhysicalQubit, gate_matrix};
use qiskit_synthesis::euler_one_qubit_decomposer::{
    EULER_BASES, EULER_BASIS_NAMES, EulerBasis, EulerBasisSet, OneQubitGateSequence,
    unitary_to_gate_sequence_inner,
};
use qiskit_util::getenv_use_multiple_threads;

// Minimum number of 1Q runs before we parallelize.
const PARALLEL_THRESHOLD: usize = 500;

fn compute_error_term_from_target(gate: &str, target: &Target, qubit: PhysicalQubit) -> f64 {
    1. - target.get_error(gate, &[qubit]).unwrap_or(0.)
}

fn compute_error_from_target_one_qubit_sequence(
    circuit: &OneQubitGateSequence,
    qubit: PhysicalQubit,
    target: Option<&Target>,
) -> (f64, usize) {
    match target {
        Some(target) => {
            let num_gates = circuit.gates.len();
            let gate_fidelities: f64 = circuit
                .gates
                .iter()
                .map(|gate| compute_error_term_from_target(gate.0.name(), target, qubit))
                .product();
            (1. - gate_fidelities, num_gates)
        }
        None => (circuit.gates.len() as f64, circuit.gates.len()),
    }
}

/// Pre-compute basis gates and Euler basis sets for all qubits that appear in the runs.
/// This moves the lazy initialization out of the main loop so it can be parallelized.
fn precompute_basis_data(
    dag: &DAGCircuit,
    runs: &[Vec<NodeIndex>],
    target: Option<&Target>,
    basis_gates: Option<&HashSet<String>>,
    global_decomposers: Option<&Vec<String>>,
) -> PyResult<(Vec<Option<HashSet<String>>>, Vec<EulerBasisSet>)> {
    let dag_qubits = dag.num_qubits();
    let mut basis_gates_per_qubit: Vec<Option<HashSet<String>>> = vec![None; dag_qubits];
    let mut target_basis_per_qubit: Vec<EulerBasisSet> = vec![EulerBasisSet::new(); dag_qubits];

    // Collect unique qubits from runs
    let mut seen_qubits: HashSet<usize> = HashSet::new();
    for raw_run in runs {
        if let NodeType::Operation(inst) = &dag[raw_run[0]] {
            let qubit = PhysicalQubit::new(dag.get_qargs(inst.qubits)[0].0);
            if seen_qubits.insert(qubit.index()) {
                // Initialize basis_gates for this qubit
                let qubit_basis = match target {
                    Some(target) => Some(
                        target
                            .operation_names_for_qargs(&[qubit])?
                            .into_iter()
                            .filter(|gate_name| {
                                let target_op = target.operation_from_name(gate_name).unwrap();
                                let TargetOperation::Normal(gate) = target_op else {
                                    return false;
                                };
                                if let OperationRef::StandardGate(_) = gate.operation.view() {
                                    gate.params_view()
                                        .iter()
                                        .all(|x| matches!(x, Param::ParameterExpression(_)))
                                } else {
                                    true
                                }
                            })
                            .map(|s| s.to_string())
                            .collect(),
                    ),
                    None => basis_gates
                        .map(|basis| basis.iter().map(|x| x.to_string()).collect()),
                };
                basis_gates_per_qubit[qubit.index()] = qubit_basis;

                // Initialize target_basis_set for this qubit
                let target_basis_set = &mut target_basis_per_qubit[qubit.index()];
                let basis_ref = basis_gates_per_qubit[qubit.index()].as_ref();
                match target {
                    Some(_target) => EULER_BASES
                        .iter()
                        .enumerate()
                        .filter_map(|(idx, gates)| {
                            if !gates.iter().all(|gate| {
                                basis_ref
                                    .expect("the target path always provides a hash set")
                                    .contains(*gate)
                            }) {
                                return None;
                            }
                            let basis = EULER_BASIS_NAMES[idx];
                            Some(basis)
                        })
                        .for_each(|basis| target_basis_set.add_basis(basis)),
                    None => match global_decomposers {
                        Some(bases) => {
                            for basis in bases.iter() {
                                target_basis_set.add_basis(EulerBasis::__new__(basis)?)
                            }
                        }
                        None => match basis_ref {
                            Some(gates) => EULER_BASES
                                .iter()
                                .enumerate()
                                .filter_map(|(idx, euler_basis_gates)| {
                                    if !gates.iter().all(|gate| {
                                        euler_basis_gates
                                            .as_ref()
                                            .contains(&gate.as_str())
                                    }) {
                                        return None;
                                    }
                                    let basis = EULER_BASIS_NAMES[idx];
                                    Some(basis)
                                })
                                .for_each(|basis| target_basis_set.add_basis(basis)),
                            None => target_basis_set.support_all(),
                        },
                    },
                };
                if target_basis_set.basis_supported(EulerBasis::U3)
                    && target_basis_set.basis_supported(EulerBasis::U321)
                {
                    target_basis_set.remove(EulerBasis::U3);
                }
                if target_basis_set.basis_supported(EulerBasis::ZSX)
                    && target_basis_set.basis_supported(EulerBasis::ZSXX)
                {
                    target_basis_set.remove(EulerBasis::ZSX);
                }
            }
        }
    }
    Ok((basis_gates_per_qubit, target_basis_per_qubit))
}

/// Result of computing a replacement for a single 1Q run.
struct RunReplacement {
    run: Vec<NodeIndex>,
    sequence: OneQubitGateSequence,
}

/// Process a single 1Q run: compute the combined matrix, decompose, and decide
/// whether the replacement is an improvement. Returns Some(RunReplacement) if
/// the run should be replaced, None if it should be kept as-is.
fn process_run(
    dag: &DAGCircuit,
    raw_run: Vec<NodeIndex>,
    target: Option<&Target>,
    basis_gates_per_qubit: &[Option<HashSet<String>>],
    target_basis_per_qubit: &[EulerBasisSet],
) -> Option<RunReplacement> {
    let qubit: PhysicalQubit = if let NodeType::Operation(inst) = &dag[raw_run[0]] {
        PhysicalQubit::new(dag.get_qargs(inst.qubits)[0].0)
    } else {
        unreachable!("nodes in runs will always be op nodes")
    };

    let mut error = match target {
        Some(_) => 1.,
        None => raw_run.len() as f64,
    };

    let target_basis_set = &target_basis_per_qubit[qubit.index()];
    let operator = raw_run
        .iter()
        .map(|node_index| {
            let node = &dag[*node_index];
            if let NodeType::Operation(inst) = node {
                if let Some(target) = target {
                    error *= compute_error_term_from_target(inst.op.name(), target, qubit);
                }
                inst.try_matrix_as_static_1q()
                    .expect("collect_1q_runs only collects gates that can produce a matrix")
            } else {
                unreachable!("Can only have op nodes here")
            }
        })
        .fold(gate_matrix::ONE_QUBIT_IDENTITY, |mut operator, node| {
            matmul_1q_with_slice(&mut operator, &node);
            operator
        });

    let old_error = if target.is_some() {
        (1. - error, raw_run.len())
    } else {
        (error, raw_run.len())
    };
    let sequence = unitary_to_gate_sequence_inner(
        aview2(&operator),
        target_basis_set,
        qubit.index(),
        None,
        true,
        None,
    );
    let sequence = match sequence {
        Some(seq) => seq,
        None => return None,
    };
    let new_error = compute_error_from_target_one_qubit_sequence(&sequence, qubit, target);

    let basis_gates = basis_gates_per_qubit[qubit.index()].as_ref();
    let mut outside_basis = false;
    if let Some(basis) = basis_gates {
        for node in &raw_run {
            if let NodeType::Operation(inst) = &dag[*node] {
                if !basis.contains(inst.op.name()) {
                    outside_basis = true;
                    break;
                }
            }
        }
    }

    if outside_basis
        || new_error < old_error
        || new_error.0.abs() < 1e-9 && old_error.0.abs() >= 1e-9
    {
        Some(RunReplacement {
            run: raw_run,
            sequence,
        })
    } else {
        None
    }
}

#[pyfunction]
#[pyo3(name = "optimize_1q_gates_decomposition", signature = (dag, *, target=None, basis_gates=None, global_decomposers=None))]
pub fn py_optimize_1q_gates_decomposition(
    py: Python,
    dag: &mut DAGCircuit,
    target: Option<&Target>,
    basis_gates: Option<HashSet<String>>,
    global_decomposers: Option<Vec<String>>,
) -> PyResult<()> {
    // Release the GIL so rayon threads can run freely
    py.detach(|| {
        run_optimize_1q_gates_decomposition(dag, target, basis_gates, global_decomposers)
    })
}

pub fn run_optimize_1q_gates_decomposition(
    dag: &mut DAGCircuit,
    target: Option<&Target>,
    basis_gates: Option<HashSet<String>>,
    global_decomposers: Option<Vec<String>>,
) -> PyResult<()> {
    let runs: Vec<Vec<NodeIndex>> = dag.collect_1q_runs().unwrap().collect();
    if runs.is_empty() {
        return Ok(());
    }

    // Pre-compute basis data for all qubits upfront (sequential, removes shared mutable state)
    let (basis_gates_per_qubit, target_basis_per_qubit) = precompute_basis_data(
        dag,
        &runs,
        target,
        basis_gates.as_ref(),
        global_decomposers.as_ref(),
    )?;

    // Compute replacements — parallel for large circuits, sequential for small ones
    let run_in_parallel = getenv_use_multiple_threads();
    let replacements: Vec<RunReplacement> = if runs.len() >= PARALLEL_THRESHOLD && run_in_parallel {
        runs.into_par_iter()
            .filter_map(|raw_run| {
                process_run(
                    dag,
                    raw_run,
                    target,
                    &basis_gates_per_qubit,
                    &target_basis_per_qubit,
                )
            })
            .collect()
    } else {
        runs.into_iter()
            .filter_map(|raw_run| {
                process_run(
                    dag,
                    raw_run,
                    target,
                    &basis_gates_per_qubit,
                    &target_basis_per_qubit,
                )
            })
            .collect()
    };

    // Apply replacements sequentially (DAG mutations are not thread-safe)
    for replacement in replacements {
        for gate in replacement.sequence.gates {
            dag.insert_1q_on_incoming_qubit((gate.0, &gate.1), replacement.run[0]);
        }
        dag.add_global_phase(&Param::Float(replacement.sequence.global_phase))?;
        dag.remove_1q_sequence(&replacement.run);
    }
    Ok(())
}

#[inline(always)]
pub(crate) fn matmul_1q(operator: &mut [[Complex64; 2]; 2], other: Array2<Complex64>) {
    *operator = [
        [
            other[[0, 0]] * operator[0][0] + other[[0, 1]] * operator[1][0],
            other[[0, 0]] * operator[0][1] + other[[0, 1]] * operator[1][1],
        ],
        [
            other[[1, 0]] * operator[0][0] + other[[1, 1]] * operator[1][0],
            other[[1, 0]] * operator[0][1] + other[[1, 1]] * operator[1][1],
        ],
    ];
}

/// Computes matrix product ``other * operator`` and stores the result within ``operator``.
#[inline(always)]
pub fn matmul_1q_with_slice(operator: &mut [[Complex64; 2]; 2], other: &[[Complex64; 2]; 2]) {
    *operator = [
        [
            other[0][0] * operator[0][0] + other[0][1] * operator[1][0],
            other[0][0] * operator[0][1] + other[0][1] * operator[1][1],
        ],
        [
            other[1][0] * operator[0][0] + other[1][1] * operator[1][0],
            other[1][0] * operator[0][1] + other[1][1] * operator[1][1],
        ],
    ];
}

pub fn optimize_1q_gates_decomposition_mod(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_wrapped(wrap_pyfunction!(py_optimize_1q_gates_decomposition))?;
    Ok(())
}
