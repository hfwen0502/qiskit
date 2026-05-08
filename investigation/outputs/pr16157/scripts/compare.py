"""PR #16157 evidence analysis — compare main vs PR benchmark JSONs.

Flags any:
- Gate/depth metric differences (regressions on correctness — bit-identical expected)
- Timing changes (PR slower = regression, PR faster = improvement)

Usage:
    python pr16157_compare.py <main.json> <pr.json> [--threshold 0.05]
"""

import argparse
import json
import sys
from pathlib import Path


METRICS = [
    "output_gate_count_2q",
    "output_gate_count_1q",
    "output_gate_count_total",
    "output_depth_2q",
    "output_depth_1q",
    "output_depth_total",
]


def load(path):
    with open(path) as f:
        data = json.load(f)
    out = {}
    for b in data["benchmarks"]:
        name = b["name"]
        extra = b.get("extra_info", {})
        out[name] = {
            "mean": b["stats"]["mean"],
            "min": b["stats"]["min"],
            "rounds": b["stats"]["rounds"],
            **{m: extra.get(m) for m in METRICS},
            "ops": extra.get("output_circuit_operations"),
        }
    return out


def short(name):
    # test_feynman_transpile[hwb12.qasm] → hwb12
    if "[" in name and name.endswith("]"):
        return name[name.index("[") + 1 : -1].replace(".qasm", "")
    return name


def main():
    p = argparse.ArgumentParser()
    p.add_argument("main_json")
    p.add_argument("pr_json")
    p.add_argument("--threshold", type=float, default=0.05, help="timing change threshold to flag (default 5%%)")
    args = p.parse_args()

    main = load(args.main_json)
    pr = load(args.pr_json)

    common = sorted(set(main) & set(pr))
    only_main = set(main) - set(pr)
    only_pr = set(pr) - set(main)

    if only_main or only_pr:
        print(f"WARNING: circuits only in main: {sorted(only_main)}")
        print(f"WARNING: circuits only in pr:   {sorted(only_pr)}")

    # Correctness: gate/depth metrics should be bit-identical
    regressions = []  # list of (name, metric, main_val, pr_val)
    op_diffs = []
    for name in common:
        m, p_ = main[name], pr[name]
        for metric in METRICS:
            if m[metric] != p_[metric]:
                regressions.append((name, metric, m[metric], p_[metric]))
        if m["ops"] != p_["ops"]:
            op_diffs.append((name, m["ops"], p_["ops"]))

    # Timing
    slowdowns, speedups = [], []
    total_main_time = 0.0
    total_pr_time = 0.0
    for name in common:
        mt, pt = main[name]["mean"], pr[name]["mean"]
        total_main_time += mt
        total_pr_time += pt
        ratio = pt / mt if mt > 0 else float("inf")
        if ratio > 1 + args.threshold:
            slowdowns.append((name, mt, pt, ratio))
        elif ratio < 1 - args.threshold:
            speedups.append((name, mt, pt, ratio))

    # Report
    print("=" * 72)
    print(f"PR #16157 evidence: main={args.main_json}  pr={args.pr_json}")
    print(f"Tests in common: {len(common)}")
    print("=" * 72)

    print("\n--- CORRECTNESS (gate/depth should be bit-identical) ---")
    if not regressions and not op_diffs:
        print(f"  PASS: all {len(common)} circuits produce identical gate counts and depths")
    else:
        print(f"  FAIL: {len(regressions)} metric mismatches across {len(set(r[0] for r in regressions))} circuits")
        for name, metric, mv, pv in regressions[:30]:
            print(f"    {short(name):30s}  {metric:25s}  main={mv}  pr={pv}  diff={pv - mv if isinstance(pv, (int, float)) and isinstance(mv, (int, float)) else '?'}")
        if len(regressions) > 30:
            print(f"    ... and {len(regressions) - 30} more")
        if op_diffs:
            print(f"  {len(op_diffs)} circuits with op-dict differences (first 5):")
            for name, mops, pops in op_diffs[:5]:
                print(f"    {short(name):30s}  main={mops}  pr={pops}")

    print("\n--- TIMING ---")
    print(f"  Total wall-clock (sum of means): main={total_main_time:.1f}s  pr={total_pr_time:.1f}s  ratio={total_pr_time/total_main_time:.3f}x ({(total_pr_time/total_main_time - 1)*100:+.1f}%)")
    print()
    if slowdowns:
        print(f"  SLOWDOWNS (>{args.threshold*100:.0f}% slower on PR):")
        for name, mt, pt, ratio in sorted(slowdowns, key=lambda x: -x[3])[:20]:
            print(f"    {short(name):30s}  main={mt:8.3f}s  pr={pt:8.3f}s  ratio={ratio:.3f}x")
    else:
        print(f"  No circuits >{args.threshold*100:.0f}% slower on PR.")

    print()
    if speedups:
        print(f"  SPEEDUPS (>{args.threshold*100:.0f}% faster on PR):")
        for name, mt, pt, ratio in sorted(speedups, key=lambda x: x[3])[:20]:
            print(f"    {short(name):30s}  main={mt:8.3f}s  pr={pt:8.3f}s  ratio={ratio:.3f}x  ({(1-ratio)*100:.1f}% faster)")
    else:
        print(f"  No circuits >{args.threshold*100:.0f}% faster on PR.")

    print()
    print(f"  Within ±{args.threshold*100:.0f}%: {len(common) - len(slowdowns) - len(speedups)} circuits")

    # exit code: 0 if no correctness regressions, 1 otherwise
    sys.exit(1 if regressions or op_diffs else 0)


if __name__ == "__main__":
    main()
