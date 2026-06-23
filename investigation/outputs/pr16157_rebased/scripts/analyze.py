#!/usr/bin/env python3
# Per-group analysis for PR #16157 (rebased) evidence.
# Compares main vs PR benchmark JSON: aggregate gate counts, depths, wall-clock.
# Writes <group>/analysis.txt and exits 3 if any regression is detected
# (any 2Q/1Q/total gate-count or depth change, or PR slower end-to-end).
#
# Usage: python analyze.py <main.json> <pr.json> <analysis.txt> <group>
import json
import sys

main_json, pr_json, out_txt, group = sys.argv[1:5]
M = {b["name"]: b for b in json.load(open(main_json))["benchmarks"]}
P = {b["name"]: b for b in json.load(open(pr_json))["benchmarks"]}
common = [n for n in M if n in P]

GATE_KEYS = ["output_gate_count_2q", "output_gate_count_1q", "output_gate_count_total"]
DEPTH_KEYS = ["output_depth_2q", "output_depth_1q", "output_depth_total"]
KEYS = GATE_KEYS + DEPTH_KEYS

agg = {k: [0, 0] for k in KEYS}
wall = [0.0, 0.0]
regress = []
for n in common:
    me, pe = M[n]["extra_info"], P[n]["extra_info"]
    for k in KEYS:
        mv, pv = me.get(k), pe.get(k)
        if mv is not None and pv is not None:
            agg[k][0] += mv
            agg[k][1] += pv
            if mv != pv:
                regress.append(f"GATE/DEPTH {n}: {k} {mv} -> {pv} (delta {pv - mv:+})")
    mm, pm = M[n]["stats"]["mean"], P[n]["stats"]["mean"]
    wall[0] += mm
    wall[1] += pm
    if pm > mm * 1.05 and (pm - mm) > 0.05:  # PR materially slower on this circuit
        regress.append(f"WALL {n}: {mm:.3f}s -> {pm:.3f}s ({(pm - mm) / mm * 100:+.1f}%)")

lines = [f"=== {group}: main vs PR ===", f"circuits compared: {len(common)}", ""]
hdr = f"{'metric':26s} {'main':>14s} {'pr':>14s} {'delta':>12s} {'pct':>9s}"
lines += [hdr, "-" * len(hdr)]
for k in KEYS:
    m, p = agg[k]
    d = p - m
    pct = (d / m * 100) if m else 0.0
    lines.append(f"{k:26s} {m:>14} {p:>14} {d:>+12} {pct:>+8.2f}%")
d = wall[1] - wall[0]
pct = (d / wall[0] * 100) if wall[0] else 0.0
lines.append(f"{'wall_clock_sum_s':26s} {wall[0]:>14.2f} {wall[1]:>14.2f} {d:>+12.2f} {pct:>+8.2f}%")
lines.append("")
if regress:
    lines.append(f"!!! {len(regress)} REGRESSION(S) DETECTED:")
    lines += [f"  - {r}" for r in regress[:30]]
else:
    lines.append("OK: gate counts + depths bit-identical; PR not slower end-to-end.")

text = "\n".join(lines) + "\n"
open(out_txt, "w").write(text)
print(text)
sys.exit(3 if regress else 0)
