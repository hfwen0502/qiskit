// This code is part of Qiskit.
//
// (C) Copyright IBM 2024
//
// This code is licensed under the Apache License, Version 2.0. You may
// obtain a copy of this license in the LICENSE.txt file in the root directory
// of this source tree or at https://www.apache.org/licenses/LICENSE-2.0.
//
// Any modifications or derivative works of this code must retain this
// copyright notice, and modified files need to carry a notice indicating
// that they have been altered from the originals.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::PyModule;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::{Bound, PyResult, Python, pyfunction, wrap_pyfunction};

use indexmap::IndexMap;
use rayon::prelude::*;
use rustworkx_core::petgraph::stable_graph::NodeIndex;

use crate::commutation_checker::CommutationChecker;
use qiskit_circuit::Qubit;
use qiskit_circuit::dag_circuit::{DAGCircuit, NodeType, Wire};
use qiskit_util::getenv_use_multiple_threads;

// Custom types to store the commutation sets and node indices,
// see the docstring below for more information.
type CommutationSet = IndexMap<Wire, Vec<Vec<NodeIndex>>, ::ahash::RandomState>;
type NodeIndices = IndexMap<(NodeIndex, Wire), usize, ::ahash::RandomState>;

// the maximum number of qubits we check commutativity for
const MAX_NUM_QUBITS: u32 = 3;

// Minimum number of qubits before we parallelize the per-qubit analysis.
const PARALLEL_THRESHOLD: usize = 100;

/// Analyze commutation relations for a single qubit wire.
///
/// Returns the commutation sets and node indices for this wire.
fn analyze_commutations_for_wire(
    dag: &DAGCircuit,
    commutation_checker: &CommutationChecker,
    approximation_degree: f64,
    qubit: u32,
) -> Result<
    (Wire, Vec<Vec<NodeIndex>>, Vec<((NodeIndex, Wire), usize)>),
    PyErr,
> {
    let wire = Wire::Qubit(Qubit(qubit));
    let mut wire_commutation_set: Vec<Vec<NodeIndex>> = Vec::new();
    let mut wire_node_indices: Vec<((NodeIndex, Wire), usize)> = Vec::new();

    for current_gate_idx in dag.nodes_on_wire(wire, false) {
        // Initialize if this is the first gate on the wire
        if wire_commutation_set.is_empty() {
            wire_commutation_set.push(vec![current_gate_idx]);
            wire_node_indices.push(((current_gate_idx, wire), 0));
            continue;
        }

        let last = wire_commutation_set.last_mut().unwrap();

        if !last.contains(&current_gate_idx) {
            let mut all_commute = true;

            for prev_gate_idx in last.iter() {
                if let (NodeType::Operation(packed_inst0), NodeType::Operation(packed_inst1)) =
                    (&dag[current_gate_idx], &dag[*prev_gate_idx])
                {
                    let op1 = packed_inst0.op.view();
                    let op2 = packed_inst1.op.view();

                    if packed_inst0.op.try_control_flow().is_some()
                        || packed_inst1.op.try_control_flow().is_some()
                    {
                        all_commute = false;
                        break;
                    }

                    let qargs1 = dag.get_qargs(packed_inst0.qubits);
                    let qargs2 = dag.get_qargs(packed_inst1.qubits);
                    let cargs1 = dag.get_cargs(packed_inst0.clbits);
                    let cargs2 = dag.get_cargs(packed_inst1.clbits);

                    all_commute = commutation_checker.commute(
                        &op1,
                        packed_inst0.params.as_deref(),
                        qargs1,
                        cargs1,
                        &op2,
                        packed_inst1.params.as_deref(),
                        qargs2,
                        cargs2,
                        None,
                        MAX_NUM_QUBITS,
                        approximation_degree,
                    )?;
                    if !all_commute {
                        break;
                    }
                } else {
                    all_commute = false;
                    break;
                }
            }

            if all_commute {
                last.push(current_gate_idx);
            } else {
                wire_commutation_set.push(vec![current_gate_idx]);
            }
        }

        wire_node_indices.push((
            (current_gate_idx, wire),
            wire_commutation_set.len() - 1,
        ));
    }

    Ok((wire, wire_commutation_set, wire_node_indices))
}

/// Compute the commutation sets for a given DAG.
///
/// We return two HashMaps:
///  * {wire: commutation_sets}: For each wire, we keep a vector of index sets, where each index
///    set contains mutually commuting nodes. Note that these include the input and output nodes
///    which do not commute with anything.
///  * {(node, wire): index}: For each (node, wire) pair we store the index indicating in which
///    commutation set the node appears on a given wire.
///
/// For example, if we have a circuit
///
///     |0> -- X -- SX -- Z (out)
///      0     2    3     4   1   <-- node indices including input (0) and output (1) nodes
///
/// Then we would have
///
///     commutation_set = {0: [[0], [2, 3], [4], [1]]}
///     node_indices = {(0, 0): 0, (1, 0): 3, (2, 0): 1, (3, 0): 1, (4, 0): 2}
///
pub fn analyze_commutations(
    dag: &DAGCircuit,
    commutation_checker: &CommutationChecker,
    approximation_degree: f64,
) -> PyResult<(CommutationSet, NodeIndices)> {
    let num_qubits = dag.num_qubits();
    let run_in_parallel = getenv_use_multiple_threads();

    // Compute per-qubit commutation analysis — parallel for large circuits
    let per_qubit_results: Vec<(Wire, Vec<Vec<NodeIndex>>, Vec<((NodeIndex, Wire), usize)>)> =
        if num_qubits >= PARALLEL_THRESHOLD && run_in_parallel {
            (0..num_qubits as u32)
                .into_par_iter()
                .map(|qubit| {
                    analyze_commutations_for_wire(
                        dag,
                        commutation_checker,
                        approximation_degree,
                        qubit,
                    )
                })
                .collect::<PyResult<Vec<_>>>()?
        } else {
            (0..num_qubits as u32)
                .map(|qubit| {
                    analyze_commutations_for_wire(
                        dag,
                        commutation_checker,
                        approximation_degree,
                        qubit,
                    )
                })
                .collect::<PyResult<Vec<_>>>()?
        };

    // Merge per-qubit results into final maps
    let total_indices: usize = per_qubit_results
        .iter()
        .map(|(_, _, indices)| indices.len())
        .sum();
    let mut commutation_set: CommutationSet =
        IndexMap::with_capacity_and_hasher(num_qubits, ::ahash::RandomState::default());
    let mut node_indices: NodeIndices =
        IndexMap::with_capacity_and_hasher(total_indices, ::ahash::RandomState::default());

    for (wire, sets, indices) in per_qubit_results {
        if !sets.is_empty() {
            commutation_set.insert(wire, sets);
        }
        for (key, val) in indices {
            node_indices.insert(key, val);
        }
    }

    Ok((commutation_set, node_indices))
}

#[pyfunction]
#[pyo3(name = "analyze_commutations", signature = (dag, commutation_checker, approximation_degree=1.))]
pub fn py_analyze_commutations(
    py: Python,
    dag: &mut DAGCircuit,
    commutation_checker: &mut CommutationChecker,
    approximation_degree: f64,
) -> PyResult<Py<PyDict>> {
    // This returns two HashMaps:
    //   * The commuting nodes per wire: {wire: [commuting_nodes_1, commuting_nodes_2, ...]}
    //   * The index in which commutation set a given node is located on a wire: {(node, wire): index}
    // The Python dict will store both of these dictionaries in one.
    let (commutation_set, node_indices) =
        analyze_commutations(dag, commutation_checker, approximation_degree)?;

    let out_dict = PyDict::new(py);

    // First set the {wire: [commuting_nodes_1, ...]} bit
    for (wire, commutations) in commutation_set {
        // we know all wires are of type Wire::Qubit, since in analyze_commutations_inner
        // we only iterate over the qubits
        let py_wire = match wire {
            Wire::Qubit(q) => dag.qubits().get(q).unwrap().into_pyobject(py),
            _ => return Err(PyValueError::new_err("Unexpected wire type.")),
        }?;

        out_dict.set_item(
            py_wire,
            PyList::new(
                py,
                commutations.iter().map(|inner| {
                    PyList::new(
                        py,
                        inner
                            .iter()
                            .map(|node_index| dag.get_node(py, *node_index).unwrap()),
                    )
                    .unwrap()
                }),
            )?,
        )?;
    }

    // Then we add the {(node, wire): index} dictionary
    for ((node_index, wire), index) in node_indices {
        let py_wire = match wire {
            Wire::Qubit(q) => dag.qubits().get(q).unwrap().into_pyobject(py),
            _ => return Err(PyValueError::new_err("Unexpected wire type.")),
        }?;
        out_dict.set_item((dag.get_node(py, node_index)?, py_wire), index)?;
    }

    Ok(out_dict.unbind())
}

pub fn commutation_analysis_mod(m: &Bound<PyModule>) -> PyResult<()> {
    m.add_wrapped(wrap_pyfunction!(py_analyze_commutations))?;
    Ok(())
}
