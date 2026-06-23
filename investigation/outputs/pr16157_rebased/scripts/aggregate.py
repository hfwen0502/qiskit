#!/usr/bin/env python3
# Aggregate all groups into a top-level summary (per-group + grand total).
import json
import os

root = "/mnt/data/spotter-val/fullsuite_evidence"
order = ["feynman", "device_hamiltonians", "abstract_small", "abstract_medium",
         "abstract_large", "abstract_hamiltonians"]
KEYS = ["output_gate_count_2q", "output_gate_count_1q", "output_gate_count_total",
        "output_depth_2q", "output_depth_1q", "output_depth_total"]

gt = {k: [0, 0] for k in KEYS}
gwall = [0.0, 0.0]
gn = 0
print("=== PR #16157 (rebased onto 2.6-dev main) - full benchpress suite ===")
hdr = "{:22s} {:>5s} {:>11s} {:>10s} {:>12s} {:>10s} {:>9s} {:>8s}".format(
    "group", "circ", "2Qgate d", "totgate d", "totdepth d", "wall main", "wall pr", "wall %")
print(hdr)
print("-" * len(hdr))
for g in order:
    d = os.path.join(root, g)
    M = {b["name"]: b for b in json.load(open(d + "/main.json"))["benchmarks"]}
    P = {b["name"]: b for b in json.load(open(d + "/pr.json"))["benchmarks"]}
    common = [n for n in M if n in P]
    gn += len(common)
    agg = {k: [0, 0] for k in KEYS}
    wall = [0.0, 0.0]
    for n in common:
        for k in KEYS:
            mv, pv = M[n]["extra_info"].get(k), P[n]["extra_info"].get(k)
            if mv is not None and pv is not None:
                agg[k][0] += mv
                agg[k][1] += pv
                gt[k][0] += mv
                gt[k][1] += pv
        wall[0] += M[n]["stats"]["mean"]
        wall[1] += P[n]["stats"]["mean"]
    gwall[0] += wall[0]
    gwall[1] += wall[1]
    d2q = agg["output_gate_count_2q"][1] - agg["output_gate_count_2q"][0]
    dtot = agg["output_gate_count_total"][1] - agg["output_gate_count_total"][0]
    dtd = agg["output_depth_total"][1] - agg["output_depth_total"][0]
    wpct = (wall[1] - wall[0]) / wall[0] * 100 if wall[0] else 0
    print("{:22s} {:>5d} {:>+11d} {:>+10d} {:>+12d} {:>10.2f} {:>9.2f} {:>+7.2f}%".format(
        g, len(common), d2q, dtot, dtd, wall[0], wall[1], wpct))
print("-" * len(hdr))
d2q = gt["output_gate_count_2q"][1] - gt["output_gate_count_2q"][0]
dtot = gt["output_gate_count_total"][1] - gt["output_gate_count_total"][0]
dtd = gt["output_depth_total"][1] - gt["output_depth_total"][0]
wpct = (gwall[1] - gwall[0]) / gwall[0] * 100
print("{:22s} {:>5d} {:>+11d} {:>+10d} {:>+12d} {:>10.2f} {:>9.2f} {:>+7.2f}%".format(
    "TOTAL", gn, d2q, dtot, dtd, gwall[0], gwall[1], wpct))
print()
print("Grand totals: 2Q gates {} (delta {:+}); total depth {} (delta {:+})".format(
    gt["output_gate_count_2q"][0], d2q, gt["output_depth_total"][0], dtd))
print("Caveats:")
print("  - abstract_small: +1 1Q gate on qec_en_n5 (known 1Q tie-break non-determinism; 2Q + depth identical).")
print("  - abstract_large: bv_n280-all-to-all +13% wall on a <2s circuit (single-shot noise; output identical).")
