"""Benchmark parallelized optimization passes.

Measures transpile time to compare upstream main vs our branch.
Supports sequential-only mode and parallel mode with configurable settings.

Usage:
    # Sequential only (default)
    python investigation/benchmark_parallel_passes.py

    # Parallel mode
    python investigation/benchmark_parallel_passes.py --parallel
"""

import argparse
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

NUM_RUNS = 5


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


def benchmark_one(kind, n, parallel):
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--parallel", action="store_true",
                        help="Run in parallel mode (QISKIT_IN_PARALLEL=TRUE)")
    args = parser.parse_args()

    mode = "parallel" if args.parallel else "sequential"
    parallel = args.parallel

    print(f"Benchmark: optimization passes ({mode} mode)")
    print(f"Runs per circuit: {NUM_RUNS}")
    print(f"CPU count: {os.cpu_count()}")
    if parallel:
        rayon = os.environ.get("RAYON_NUM_THREADS", "not set")
        print(f"RAYON_NUM_THREADS: {rayon}")
    print("=" * 80)

    results = {}
    for name, (kind, n) in CIRCUITS.items():
        print(f"\n--- {name} ---")
        r = benchmark_one(kind, n, parallel=parallel)
        print(f"  {r['mean']:.3f}s ± {r['stdev']:.3f}s  "
              f"(min {r['min']:.3f}s, {r['mean_gates']:.0f} 2Q gates)")
        print(f"  times: {[f'{t:.3f}' for t in r['times']]}")
        results[name] = r

    # Summary table
    print("\n" + "=" * 80)
    print(f"{'Circuit':<20} {'Mean (s)':>10} {'Stdev':>8} {'Min (s)':>10} {'2Q gates':>10}")
    print("-" * 58)
    for name in CIRCUITS:
        r = results[name]
        print(f"{name:<20} {r['mean']:>10.3f} {r['stdev']:>8.3f} {r['min']:>10.3f} {r['mean_gates']:>10.0f}")


if __name__ == "__main__":
    main()
