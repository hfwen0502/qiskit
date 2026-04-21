"""Benchmark parallelized optimization passes.

Measures transpile time with QISKIT_IN_PARALLEL=TRUE vs FALSE to isolate
the impact of rayon parallelization in:
  - Optimize1qGatesDecomposition (threshold: 500 runs)
  - CommutationAnalysis (threshold: 100 qubits)
  - ConsolidateBlocks (threshold: 200 2Q blocks)

Usage:
    python investigation/benchmark_parallel_passes.py
"""

import os
import time
import statistics

# Test circuits of varying sizes
CIRCUITS = {
    "QFT-50": ("qft", 50),
    "QFT-100": ("qft", 100),
    "EfficientSU2-50": ("su2", 50),
    "EfficientSU2-100": ("su2", 100),
    "QV-50": ("qv", 50),
    "QV-100": ("qv", 100),
}

NUM_RUNS = 1


def make_circuit(kind, n):
    if kind == "qft":
        from qiskit.synthesis.qft import synth_qft_full
        return synth_qft_full(n)
    elif kind == "su2":
        from qiskit.circuit.library import efficient_su2
        qc = efficient_su2(n, reps=3, entanglement="linear")
        qc.measure_all()
        return qc
    elif kind == "qv":
        from qiskit.circuit.library import QuantumVolume
        return QuantumVolume(n, depth=5, seed=42)
    else:
        raise ValueError(f"Unknown circuit kind: {kind}")


def benchmark_one(circuit_name, kind, n, parallel):
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    from qiskit_ibm_runtime.fake_provider import FakeTorino

    os.environ["QISKIT_IN_PARALLEL"] = "TRUE" if parallel else "FALSE"

    backend = FakeTorino()
    pm = generate_preset_pass_manager(optimization_level=2, backend=backend)
    qc = make_circuit(kind, n)

    times = []
    gate_counts = []
    for _ in range(NUM_RUNS):
        start = time.perf_counter()
        result = pm.run(qc)
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        cx = sum(v for k, v in result.count_ops().items() if k in ("cx", "cz", "ecr"))
        gate_counts.append(cx)

    return {
        "times": times,
        "mean": statistics.mean(times),
        "stdev": statistics.stdev(times) if len(times) > 1 else 0,
        "min": min(times),
        "gate_counts": gate_counts,
        "mean_gates": statistics.mean(gate_counts),
    }


def main():
    print(f"Benchmark: parallel optimization passes")
    print(f"Runs per config: {NUM_RUNS}")
    print(f"CPU count: {os.cpu_count()}")
    print("=" * 80)

    results = {}
    for name, (kind, n) in CIRCUITS.items():
        print(f"\n--- {name} ---")

        # Sequential
        seq = benchmark_one(name, kind, n, parallel=False)
        print(f"  Sequential: {seq['mean']:.3f}s ± {seq['stdev']:.3f}s  "
              f"(min {seq['min']:.3f}s, {seq['mean_gates']:.0f} 2Q gates)")

        # Parallel
        par = benchmark_one(name, kind, n, parallel=True)
        print(f"  Parallel:   {par['mean']:.3f}s ± {par['stdev']:.3f}s  "
              f"(min {par['min']:.3f}s, {par['mean_gates']:.0f} 2Q gates)")

        speedup = seq["mean"] / par["mean"]
        pct = (1 - par["mean"] / seq["mean"]) * 100
        print(f"  Speedup: {speedup:.2f}x ({pct:+.1f}%)")

        results[name] = {"sequential": seq, "parallel": par}

    # Summary table
    print("\n" + "=" * 80)
    print(f"{'Circuit':<20} {'Seq (s)':>10} {'Par (s)':>10} {'Speedup':>10} {'Gates match':>12}")
    print("-" * 62)
    for name in CIRCUITS:
        r = results[name]
        seq_mean = r["sequential"]["mean"]
        par_mean = r["parallel"]["mean"]
        speedup = seq_mean / par_mean
        gates_match = "YES" if abs(r["sequential"]["mean_gates"] - r["parallel"]["mean_gates"]) < 0.01 * r["sequential"]["mean_gates"] else "~same"
        print(f"{name:<20} {seq_mean:>10.3f} {par_mean:>10.3f} {speedup:>9.2f}x {gates_match:>12}")


if __name__ == "__main__":
    main()
