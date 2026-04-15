// This code is part of Qiskit.
//
// (C) Copyright IBM 2022
//
// This code is licensed under the Apache License, Version 2.0. You may
// obtain a copy of this license in the LICENSE.txt file in the root directory
// of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
//
// Any modifications or derivative works of this code must retain this
// copyright notice, and modified files need to carry a notice indicating
// that they have been altered from the originals.

use std::collections::VecDeque;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use hashbrown::HashSet;
use ndarray::aview2;
use rand::prelude::*;
use rand_pcg::Pcg64Mcg;
use rayon_cond::CondIterator;
use rustworkx_core::petgraph::graph::NodeIndex;

use qiskit_circuit::dag_circuit::DAGCircuit;
use qiskit_circuit::nlayout::NLayout;
use qiskit_circuit::{BlocksMode, PhysicalQubit, VirtualQubit, getenv_use_multiple_threads};

use crate::TranspilerError;
use crate::neighbors::Neighbors;
use crate::passes::{
    dense_layout,
    disjoint_layout::{self, DisjointSplit},
};
use crate::target::{Target, TargetCouplingError};

use super::dag::{InteractionKind, SabreDAG};
use super::heuristic::Heuristic;
use super::route::{RoutingProblem, RoutingResult, RoutingTarget, swap_map, swap_map_trial};

#[allow(clippy::too_many_arguments)]
#[pyfunction]
#[pyo3(signature = (dag, target, heuristic, max_iterations, num_swap_trials, num_random_trials, seed=None, partial_layouts=vec![], skip_routing=false))]
pub fn sabre_layout_and_routing(
    dag: &mut DAGCircuit,
    target: &Target,
    heuristic: &Heuristic,
    max_iterations: usize,
    num_swap_trials: usize,
    num_random_trials: usize,
    seed: Option<u64>,
    partial_layouts: Vec<Vec<Option<PhysicalQubit>>>,
    skip_routing: bool,
) -> PyResult<(DAGCircuit, NLayout, NLayout)> {
    let Some(num_physical_qubits) = target.num_qubits else {
        return Err(TranspilerError::new_err(
            "given 'Target' was not initialized with a qubit count",
        ));
    };
    let num_physical_qubits = num_physical_qubits as usize;
    if partial_layouts
        .iter()
        .flatten()
        .any(|q| q.is_some_and(|q| q.index() >= num_physical_qubits))
    {
        return Err(TranspilerError::new_err(
            "partial layouts contained out-of-range physical qubits",
        ));
    }
    let allow_parallel = getenv_use_multiple_threads();
    let coupling = match target.coupling_graph() {
        Ok(coupling) => coupling,
        Err(TargetCouplingError::AllToAll) => {
            let mut out = dag.clone();
            out.make_physical(num_physical_qubits);
            let trivial = NLayout::generate_trivial_layout(num_physical_qubits as u32);
            return Ok((out, trivial.clone(), trivial));
        }
        Err(e @ TargetCouplingError::MultiQ(_)) => {
            return Err(TranspilerError::new_err(e.to_string()));
        }
    };
    let mut starting_layouts = (0..num_random_trials)
        .map(|_| Vec::new())
        .collect::<Vec<_>>();
    let seeds = |count| {
        match seed {
            Some(seed) => Pcg64Mcg::seed_from_u64(seed),
            None => Pcg64Mcg::from_os_rng(),
        }
        .sample_iter(&rand::distr::StandardUniform)
        .take(count)
        .collect::<Vec<_>>()
    };
    fn expand_layout(
        num_qubits: u32,
        layout: &NLayout,
        mut qubit_fn: impl FnMut(PhysicalQubit) -> PhysicalQubit,
    ) -> NLayout {
        debug_assert!(layout.num_qubits() <= num_qubits as usize);
        let max = VirtualQubit(u32::MAX);
        let mut virtuals = vec![max; num_qubits as usize];
        for (virt, phys) in layout.iter_virtual() {
            virtuals[qubit_fn(phys).index()] = virt;
        }
        let first_ancilla = layout.num_qubits() as u32;
        for (offset, virt) in virtuals.iter_mut().filter(|virt| **virt == max).enumerate() {
            *virt = VirtualQubit(first_ancilla + offset as u32);
        }
        NLayout::from_physical_to_virtual(virtuals).expect("all indices are valid")
    }
    enum TargetSplit {
        // The DAG all fits into a single connected chip in the target (maybe a subset).
        Single(Option<Vec<PhysicalQubit>>),
        // The DAG has to be split into separate chips.
        Multiple(Vec<disjoint_layout::DisjointComponent>),
    }
    let components = match disjoint_layout::distribute_components(dag, target)? {
        DisjointSplit::NoneNeeded => TargetSplit::Single(None),
        DisjointSplit::TargetSubset(subset) => TargetSplit::Single(Some(subset)),
        DisjointSplit::Arbitrary(components) => TargetSplit::Multiple(components),
    };
    let sabre_full = SabreDAG::from_dag(dag)?;
    match components {
        TargetSplit::Single(mut subset) => {
            // All the DAG fits into a single component of a disjoint `Target`, so we can safely
            // continue with the entire layout and routing, providing we stay within the subset of
            // the `Target` (if any).
            let neighbors = match subset.as_deref_mut() {
                Some(subset) => {
                    // TODO: currently, the `subset` we get from `disjoint_layout` has a
                    // non-deterministic order.  Sorting it is a canonicalisation step, but the
                    // exact order doesn't matter.  Sorted order happens to cause us to be RNG
                    // compatible with the prior Sabre disjoint handling.
                    subset.sort_unstable();
                    Neighbors::from_coupling_subset_with_map(&coupling, subset, |q| {
                        NodeIndex::new(q.index())
                    })
                }
                None => Neighbors::from_coupling(&coupling),
            };
            let target = RoutingTarget::from_neighbors(neighbors);
            let problem = RoutingProblem {
                target: &target,
                sabre: &sabre_full,
                dag,
                heuristic,
            };
            starting_layouts.extend(partial_layouts);
            add_heuristic_layouts(&mut starting_layouts, problem, allow_parallel);
            let num_layout_trials = starting_layouts.len();
            let (_, result) = CondIterator::new(
                seeds(num_layout_trials),
                allow_parallel && num_layout_trials > 1,
            )
            .enumerate()
            .map(|(index, seed)| {
                (
                    index,
                    layout_trial(
                        problem,
                        seed,
                        max_iterations,
                        num_swap_trials,
                        allow_parallel && num_swap_trials > 1,
                        &starting_layouts[index],
                    ),
                )
            })
            .min_by_key(|(index, result)| (result.swap_count(), *index))
            .expect("should have at least one layout trial");
            let num_swaps = result.swap_count();
            let out = dag.physical_empty_like_with_capacity(
                num_physical_qubits,
                dag.num_ops() + num_swaps,
                dag.dag().edge_count() + 2 * num_swaps,
                BlocksMode::Drop,
            )?;
            let qubit_fn = |q: PhysicalQubit| {
                subset
                    .as_deref()
                    .map_or(q, |subset: &[PhysicalQubit]| subset[q.index()])
            };
            let out = if skip_routing {
                out
            } else {
                result.rebuild_onto(out, qubit_fn)?
            };
            Ok((
                out,
                expand_layout(num_physical_qubits as u32, &result.initial_layout, qubit_fn),
                expand_layout(num_physical_qubits as u32, &result.final_layout, qubit_fn),
            ))
        }
        TargetSplit::Multiple(components) => {
            // The DAG needs splitting across multiple chips.  We can build an initial layout
            // safely, but the final routing needs to be done altogether, with cross-chip
            // synchronisation points (e.g. barriers, classical communication, etc) fully in place.
            let mut full_layout = vec![PhysicalQubit::new(u32::MAX); dag.num_qubits()];
            // Mapping of the "proper" (full-target) physical qubits to the "fake" restricted
            // physical qubit index used in the disjoint handling.  At the end of the loop, there
            // will be duplicates in the list, and there may still be un-set entries, but that
            // doesn't matter, because we only access physical qubits that come up.
            let mut sub_from_full = vec![PhysicalQubit::new(u32::MAX); num_physical_qubits];
            for component in &components {
                let sabre = SabreDAG::from_dag(&component.sub_dag)?;
                let target =
                    RoutingTarget::from_neighbors(Neighbors::from_coupling_subset_with_map(
                        &coupling,
                        &component.physical_qubits,
                        |q| NodeIndex::new(q.index()),
                    ));
                let sub_problem = RoutingProblem {
                    target: &target,
                    sabre: &sabre,
                    dag: &component.sub_dag,
                    heuristic,
                };
                for (sub, full) in component.physical_qubits.iter().enumerate() {
                    sub_from_full[full.index()] = PhysicalQubit::new(sub as u32);
                }
                let mut starting_layouts = starting_layouts.clone();
                for partial in partial_layouts.iter() {
                    let assigned_physical = |v: &VirtualQubit| {
                        partial
                            .get(v.index())
                            .copied()
                            .flatten()
                            .map(|p| {
                                let sub = sub_from_full[p.index()];
                                if component.physical_qubits[sub.index()] == p {
                                    Ok(sub)
                                } else {
                                    // TODO: this handling sucks, but it's better than panicking
                                    // later in Sabre routing when nothing makes any sense.
                                    Err(PyValueError::new_err(format!(
                                        "A custom starting layout assigned virtual qubit {} \
                                        to physical qubit {}, which could not be satisfied on \
                                        this disjoint QPU.  This might be a bug in Qiskit, or \
                                        a bug in a custom transpiler pass that set the partial \
                                        layout trials for SabreLayout.",
                                        v.index(),
                                        p.index(),
                                    )))
                                }
                            })
                            .transpose()
                    };
                    let mapped_partial = component
                        .virtual_qubits
                        .iter()
                        .map(assigned_physical)
                        .collect::<PyResult<Vec<_>>>()?;
                    starting_layouts.push(mapped_partial);
                }
                add_heuristic_layouts(&mut starting_layouts, sub_problem, allow_parallel);
                let num_layout_trials = starting_layouts.len();
                let (_, result) = CondIterator::new(
                    seeds(num_layout_trials),
                    allow_parallel && num_layout_trials > 1,
                )
                .enumerate()
                .map(|(index, seed)| {
                    (
                        index,
                        layout_trial(
                            sub_problem,
                            seed,
                            max_iterations,
                            num_swap_trials,
                            allow_parallel && num_layout_trials == 1,
                            &starting_layouts[index],
                        ),
                    )
                })
                .min_by_key(|(index, result)| (result.swap_count(), *index))
                .expect("should have at least one layout trial");
                for ((_, sub_phys), virt) in result
                    .initial_layout
                    .iter_virtual()
                    // This zip might be shorter than `initial_layout`, but we _want_ the
                    // side-effect of truncating to the non-ancillas.
                    .zip(&component.virtual_qubits)
                {
                    full_layout[virt.index()] = component.physical_qubits[sub_phys.index()];
                }
            }
            let max_virt = VirtualQubit::new(u32::MAX);
            let max_phys = PhysicalQubit::new(u32::MAX);
            let mut initial_physical = vec![max_virt; num_physical_qubits];
            for (virt, phys) in full_layout.iter().enumerate() {
                // It's possible that not `virt` has been assigned a physical qubit; this can happen
                // if the input DAG contained qubits that weren't used at all.  In this case, we can
                // treat this as if they're ancillas.
                if *phys == max_phys {
                    continue;
                }
                initial_physical[phys.index()] = VirtualQubit::new(virt as u32);
            }
            full_layout
                .iter()
                .enumerate()
                // Loop through unassigned virtual qubits and ancillas to make us full width...
                .filter_map(|(i, p)| (*p == max_phys).then_some(VirtualQubit::new(i as u32)))
                .chain((dag.num_qubits()..num_physical_qubits).map(|i| VirtualQubit::new(i as u32)))
                // ...and assign them to the unassigned physical qubits in increasing order of both.
                .zip(initial_physical.iter_mut().filter(|v| **v == max_virt))
                .for_each(|(v, slot)| *slot = v);
            let target = RoutingTarget::from_neighbors(Neighbors::from_coupling(&coupling));
            let problem = RoutingProblem {
                target: &target,
                sabre: &sabre_full,
                dag,
                heuristic,
            };
            let initial_layout =
                NLayout::from_physical_to_virtual(initial_physical).expect("all indices are valid");
            if skip_routing {
                Ok((
                    dag.physical_empty_like_with_capacity(
                        num_physical_qubits,
                        0,
                        0,
                        BlocksMode::Drop,
                    )?,
                    initial_layout.clone(),
                    initial_layout,
                ))
            } else {
                let result = swap_map(
                    problem,
                    &initial_layout,
                    seed,
                    num_swap_trials,
                    Some(allow_parallel),
                );
                Ok((
                    result.rebuild()?,
                    result.initial_layout,
                    result.final_layout,
                ))
            }
        }
    }
}

fn layout_trial<'a>(
    problem: RoutingProblem<'a>,
    seed: u64,
    max_iterations: usize,
    num_swap_trials: usize,
    run_swap_in_parallel: bool,
    starting_layout: &'_ [Option<PhysicalQubit>],
) -> RoutingResult<'a> {
    let num_physical_qubits: u32 = problem.target.neighbors.num_qubits().try_into().unwrap();
    let mut rng = Pcg64Mcg::seed_from_u64(seed);

    // This is purely for RNG compatibility during a refactor.
    let routing_seed = Pcg64Mcg::seed_from_u64(seed).next_u64();

    // Pick a random initial layout including a full ancilla allocation.
    let initial_layout = {
        let physical_qubits: Vec<PhysicalQubit> = if !starting_layout.is_empty() {
            let used_bits: HashSet<PhysicalQubit> = starting_layout
                .iter()
                .filter_map(|x| x.as_ref())
                .copied()
                .collect();
            let mut free_bits: Vec<PhysicalQubit> = (0..num_physical_qubits)
                .map(PhysicalQubit::new)
                .filter(|x| !used_bits.contains(x))
                .collect();
            free_bits.shuffle(&mut rng);
            (0..num_physical_qubits)
                .map(|x| match starting_layout.get(x as usize) {
                    Some(phys) => phys.unwrap_or_else(|| free_bits.pop().unwrap()),
                    None => free_bits.pop().unwrap(),
                })
                .collect()
        } else {
            let mut physical_qubits: Vec<PhysicalQubit> =
                (0..num_physical_qubits).map(PhysicalQubit::new).collect();
            physical_qubits.shuffle(&mut rng);
            physical_qubits
        };
        NLayout::from_virtual_to_physical(physical_qubits).unwrap()
    };

    // Sabre routing currently enforces that control-flow blocks return to their starting layout,
    // which means they don't actually affect any heuristics that affect our layout choice.
    let sabre_forwards = problem.sabre.only_interactions();
    let sabre_backwards = sabre_forwards.reverse_dag();
    let initial_layout = (0..max_iterations)
        .flat_map(|_| [&sabre_forwards, &sabre_backwards])
        .fold(initial_layout, |initial, sabre| {
            swap_map_trial(problem.with_sabre(sabre), &initial, routing_seed).final_layout
        });
    // Remap implicit ancillas to be assigned in numerical order.  This is pretty meaningless, but
    // ensures we have exact RNG compatibility with previous versions of Sabre.
    let initial_layout = {
        let first_ancilla = problem.dag.num_qubits();
        let (mut virt_to_phys, mut phys_to_virt) = initial_layout.take();
        let ancillas = &mut virt_to_phys[first_ancilla..];
        ancillas.sort_unstable_by_key(|q| q.index());
        for (offset, phys) in ancillas.iter().enumerate() {
            phys_to_virt[phys.index()] = VirtualQubit((first_ancilla + offset) as u32);
        }
        NLayout::from_vecs_unchecked(virt_to_phys, phys_to_virt)
    };
    swap_map(
        problem,
        &initial_layout,
        Some(seed),
        num_swap_trials,
        Some(run_swap_in_parallel),
    )
}

fn compute_dense_starting_layout(
    num_qubits: usize,
    target: &RoutingTarget,
    run_in_parallel: bool,
) -> Vec<Option<PhysicalQubit>> {
    let mut adj_matrix = target.distance.to_owned();
    if run_in_parallel {
        adj_matrix.par_mapv_inplace(|x| if x == 1. { 1. } else { 0. });
    } else {
        adj_matrix.mapv_inplace(|x| if x == 1. { 1. } else { 0. });
    }
    let [_rows, _cols, map] = dense_layout::best_subset(
        num_qubits,
        adj_matrix.view(),
        0,
        0,
        false,
        true,
        aview2(&[[0.]]),
    );
    map.into_iter()
        .map(|x| Some(PhysicalQubit::new(x as u32)))
        .collect()
}

/// Find a long simple path through the coupling graph.
///
/// Uses a greedy DFS with Warnsdorff-like heuristic: at each step, move to the unvisited
/// neighbor that has the fewest unvisited neighbors of its own. This produces near-Hamiltonian
/// paths on grid-like graphs (e.g., 118 out of 120 nodes on a 10x12 square lattice).
///
/// Starts from a peripheral node found via double-BFS to maximize path length.
///
/// This replaces the hard-coded ring arrays for 127/133/156-qubit devices, generalizing to any
/// coupling topology.
fn find_long_path(neighbors: &Neighbors) -> Vec<PhysicalQubit> {
    let n = neighbors.num_qubits();
    if n == 0 {
        return Vec::new();
    }

    // BFS to find a peripheral (farthest) node from a given start
    let bfs_farthest = |start: PhysicalQubit| -> PhysicalQubit {
        let mut visited = vec![false; n];
        let mut queue = VecDeque::new();
        visited[start.index()] = true;
        queue.push_back(start);
        let mut farthest = start;
        while let Some(node) = queue.pop_front() {
            farthest = node;
            for &neighbor in &neighbors[node] {
                if !visited[neighbor.index()] {
                    visited[neighbor.index()] = true;
                    queue.push_back(neighbor);
                }
            }
        }
        farthest
    };

    // Find a peripheral starting node via double-BFS
    let u = bfs_farthest(PhysicalQubit::new(0));
    let start = bfs_farthest(u);

    // Greedy DFS with Warnsdorff heuristic: always visit the neighbor with
    // the fewest remaining unvisited neighbors
    let mut visited = vec![false; n];
    let mut path = Vec::with_capacity(n);
    visited[start.index()] = true;
    path.push(start);

    loop {
        let current = *path.last().unwrap();
        // Among unvisited neighbors, pick the one with fewest unvisited neighbors
        let next = neighbors[current]
            .iter()
            .copied()
            .filter(|nb| !visited[nb.index()])
            .min_by_key(|nb| {
                neighbors[*nb]
                    .iter()
                    .filter(|nn| !visited[nn.index()])
                    .count()
            });
        match next {
            Some(nb) => {
                visited[nb.index()] = true;
                path.push(nb);
            }
            None => break,
        }
    }
    path
}

/// Detect whether the circuit's 2Q interaction graph forms a simple path (chain) or cycle (ring).
///
/// Returns `Some(num_qubits_in_structure)` if the 2Q interactions form a path or ring,
/// `None` otherwise. Both chain and ring circuits benefit from the same layout strategy:
/// mapping virtual qubits sequentially onto a long hardware path.
fn detect_path_or_ring(sabre: &SabreDAG) -> Option<usize> {
    // Collect unique 2Q edges and count degree per virtual qubit
    let mut max_qubit: u32 = 0;
    for node_idx in sabre.dag.node_indices() {
        if let InteractionKind::TwoQ([a, b]) = &sabre.dag[node_idx].kind {
            max_qubit = max_qubit.max(a.index() as u32).max(b.index() as u32);
        }
    }

    let n = (max_qubit + 1) as usize;
    if n < 2 {
        return None;
    }
    let mut deg = vec![0u32; n];
    let mut unique_edges: HashSet<(u32, u32)> = HashSet::new();
    for node_idx in sabre.dag.node_indices() {
        if let InteractionKind::TwoQ([a, b]) = &sabre.dag[node_idx].kind {
            let (a, b) = (a.index() as u32, b.index() as u32);
            let edge = (a.min(b), a.max(b));
            if unique_edges.insert(edge) {
                deg[a as usize] += 1;
                deg[b as usize] += 1;
            }
        }
    }

    // Count vertices by degree (only consider qubits that participate in 2Q gates)
    let mut num_deg1 = 0usize;
    let mut num_deg2 = 0usize;
    let mut num_active = 0usize;
    for &d in &deg {
        if d == 0 {
            continue;
        }
        num_active += 1;
        match d {
            1 => num_deg1 += 1,
            2 => num_deg2 += 1,
            _ => return None, // Degree > 2 means not a path or ring
        }
    }

    if num_active < 2 {
        return None;
    }

    // Chain: exactly 2 endpoints (deg 1), rest are deg 2
    // Ring: all vertices deg 2
    // Both require: num_edges == num_active (ring) or num_active - 1 (chain)
    let num_edges = unique_edges.len();
    if num_deg1 == 2 && num_deg2 == num_active - 2 && num_edges == num_active - 1 {
        // Chain/path structure
        Some(num_active)
    } else if num_deg1 == 0 && num_deg2 == num_active && num_edges == num_active {
        // Ring/cycle structure
        Some(num_active)
    } else {
        None
    }
}

/// Add any extra starting layouts we want to try by default, based on best guesses of what might
/// work well.
fn add_heuristic_layouts(
    starting_layouts: &mut Vec<Vec<Option<PhysicalQubit>>>,
    problem: RoutingProblem,
    run_in_parallel: bool,
) {
    let lift = |i| Some(PhysicalQubit::new(i));
    let num_physical_qubits = problem.target.neighbors.num_qubits();
    // Run a dense layout trial
    starting_layouts.push(compute_dense_starting_layout(
        problem.dag.num_qubits(),
        problem.target,
        run_in_parallel,
    ));
    starting_layouts.push((0..num_physical_qubits as u32).map(lift).collect());
    starting_layouts.push((0..num_physical_qubits as u32).rev().map(lift).collect());

    // Compute a long path through the coupling graph dynamically.
    // This generalizes the previous hard-coded rings for 127/133/156-qubit IBM devices
    // to work on any hardware topology.
    let long_path = find_long_path(&problem.target.neighbors);
    starting_layouts.push(long_path.iter().copied().map(Some).collect());

    // Circuit-aware layout: if the circuit's 2Q interaction graph forms a chain or ring,
    // map it directly onto the hardware long path. This avoids the catastrophic overhead
    // that occurs when SABRE starts from a random layout for path/ring circuits on
    // constrained topologies (especially bipartite graphs where VF2 fails on odd rings).
    if let Some(path_len) = detect_path_or_ring(&problem.sabre) {
        if path_len <= long_path.len() {
            let mut layout: Vec<Option<PhysicalQubit>> = vec![None; num_physical_qubits];
            for (i, &phys) in long_path.iter().enumerate().take(path_len) {
                layout[i] = Some(phys);
            }
            starting_layouts.push(layout);
        }
    }
}
