#!/usr/bin/env python3
# Aggregate the overnight N-iteration repeat: per-group + grand-total wall-clock
# mean/std across iterations, and gate/depth stability across iterations.
import json
import os
import statistics
import sys

out = sys.argv[1]
iters = sorted(d for d in os.listdir(out) if d.startswith("iter_"))
groups = ("feynman device_hamiltonians abstract_small abstract_medium "
          "abstract_large abstract_hamiltonians").split()


def load(it, g, side):
    p = os.path.join(out, it, g, f"{side}.json")
    if not (os.path.exists(p) and os.path.getsize(p) > 0):
        return {}
    try:
        return {b["name"]: b for b in json.load(open(p))["benchmarks"]}
    except Exception:
        return {}


def stdev(xs):
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


print(f"Overnight full-suite repeat — {len(iters)} iterations: {iters}\n")
print(f"{'group':22s} {'main wall (s)':>20s} {'PR wall (s)':>20s} {'speedup':>9s} {'PR<main':>8s}")
print("-" * 84)
gt_main = {i: 0.0 for i in iters}
gt_pr = {i: 0.0 for i in iters}
for g in groups:
    mw, pw = [], []
    for it in iters:
        M, P = load(it, g, "main"), load(it, g, "pr")
        common = [n for n in M if n in P]
        m = sum(M[n]["stats"]["mean"] for n in common)
        p = sum(P[n]["stats"]["mean"] for n in common)
        mw.append(m); pw.append(p); gt_main[it] += m; gt_pr[it] += p
    mm, pm = statistics.mean(mw), statistics.mean(pw)
    sp = (pm - mm) / mm * 100 if mm else 0
    nf = sum(1 for a, b in zip(mw, pw) if b < a)
    print(f"{g:22s} {mm:>11.2f} ± {stdev(mw):>5.2f} {pm:>12.2f} ± {stdev(pw):>5.2f} "
          f"{sp:>+7.2f}% {nf:>5d}/{len(iters)}")
print("-" * 84)
tm = [gt_main[i] for i in iters]
tp = [gt_pr[i] for i in iters]
mm, pm = statistics.mean(tm), statistics.mean(tp)
print(f"{'GRAND TOTAL':22s} {mm:>11.2f} ± {stdev(tm):>5.2f} {pm:>12.2f} ± {stdev(tp):>5.2f} "
      f"{(pm - mm) / mm * 100:>+7.2f}% {sum(1 for a, b in zip(tm, tp) if b < a):>5d}/{len(iters)}")
print(f"\nper-iteration grand-total wall (s): main {[round(x, 1) for x in tm]}")
print(f"                                    PR   {[round(x, 1) for x in tp]}")

print("\n=== gate/depth stability across iterations ===")
KEYS = ["output_gate_count_2q", "output_gate_count_1q", "output_gate_count_total", "output_depth_total"]
vary = {k: set() for k in KEYS}
mism = 0
total_circ = 0
for g in groups:
    perc = {}
    for it in iters:
        M, P = load(it, g, "main"), load(it, g, "pr")
        for n in M:
            if n not in P:
                continue
            d = perc.setdefault(n, {"m": {k: set() for k in KEYS}, "p": {k: set() for k in KEYS}})
            for k in KEYS:
                mv, pv = M[n]["extra_info"].get(k), P[n]["extra_info"].get(k)
                if mv is not None:
                    d["m"][k].add(mv)
                if pv is not None:
                    d["p"][k].add(pv)
                if mv is not None and pv is not None and mv != pv:
                    mism += 1
    total_circ += len(perc)
    for n, d in perc.items():
        for k in KEYS:
            if len(d["m"][k]) > 1 or len(d["p"][k]) > 1:
                vary[k].add(f"{g}: {n}")
print(f"circuits compared: {total_circ}")
for k in KEYS:
    print(f"  {k:24s}: {len(vary[k])} circuit(s) vary across iterations")
allvary = sorted(set().union(*vary.values()))
if allvary:
    print("  varying circuits (expected: 1Q/total only — pre-existing non-determinism):")
    for c in allvary:
        print(f"    {c}")
print(f"\nmain-vs-PR gate/depth mismatches (all iters x circuits x metrics): {mism}")
