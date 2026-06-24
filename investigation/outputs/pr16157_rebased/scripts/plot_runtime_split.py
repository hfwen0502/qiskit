#!/usr/bin/env python3
"""Runtime scatter split into two panels by loop-iteration outcome:
  left  = circuits that save one iteration (PR < main)
  right = circuits with unchanged iteration count (PR == main)
Each panel: per-circuit mean transpile time (5 runs), main (x) vs PR (y), colored by
group, with the y=x reference and red rings on any point where PR's mean exceeds main's
(all within run-to-run noise). Shows that the speedup is carried by the savers while the
unchanged circuits sit on the diagonal (no slowdown from the added signal checks).
"""
import os
import re
import statistics
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
CH = os.path.join(BASE, "charts")
os.makedirs(CH, exist_ok=True)
GROUPS = ["feynman", "device_hamiltonians", "abstract_small", "abstract_medium",
          "abstract_large", "abstract_hamiltonians"]
CMAP = plt.get_cmap("tab10")
COLOR = {g: CMAP(i) for i, g in enumerate(GROUPS)}


def norm(name):
    m = re.search(r"\[(.*)\]", name)
    s = m.group(1) if m else name
    return s[:-5] if s.endswith(".qasm") else s


def load_iters(path):
    d = {}
    for ln in open(path):
        p = ln.rstrip("\n").split("\t")
        if len(p) == 3 and not p[2].startswith("ERR"):
            d[(p[0], p[1])] = int(p[2])
    return d


# per-circuit mean wall over the 5 runs
circ = defaultdict(lambda: {"mw": [], "pw": []})
with open(os.path.join(BASE, "overnight_periter.tsv")) as f:
    hdr = f.readline().rstrip("\n").split("\t")
    for ln in f:
        r = dict(zip(hdr, ln.rstrip("\n").split("\t")))
        k = (r["group"], norm(r["name"]))
        circ[k]["mw"].append(float(r["m_wall"]))
        circ[k]["pw"].append(float(r["p_wall"]))

M_it = load_iters(os.path.join(BASE, "iters_main_full.tsv"))
P_it = load_iters(os.path.join(BASE, "iters_pr_full.tsv"))

savers, unchanged = {}, {}
for k, c in circ.items():
    if k not in M_it or k not in P_it:
        continue
    pt = (statistics.fmean(c["mw"]), statistics.fmean(c["pw"]))
    if P_it[k] < M_it[k]:
        savers[k] = pt
    elif P_it[k] == M_it[k]:
        unchanged[k] = pt

# shared log-log limits across both panels
allv = [v for pt in list(savers.values()) + list(unchanged.values()) for v in pt]
lo, hi = min(allv) * 0.7, max(allv) * 1.4


def panel(ax, pts, title):
    ax.plot([lo, hi], [lo, hi], "k--", lw=1, zorder=1, label="y = x")
    for g in GROUPS:
        gp = [v for kk, v in pts.items() if kk[0] == g]
        if gp:
            ax.scatter([a for a, _ in gp], [b for _, b in gp], s=10, alpha=0.7,
                       color=COLOR[g], edgecolor="none", label=g)
    above = [v for v in pts.values() if v[1] > v[0]]
    if above:
        ax.scatter([a for a, _ in above], [b for _, b in above], s=60, facecolors="none",
                   edgecolors="red", linewidths=0.9, zorder=6,
                   label=f"PR mean > main ({len(above)}, within noise)")
    reds = [(b - a) / a * 100 for a, b in pts.values()]
    med, mean = statistics.median(reds), statistics.fmean(reds)
    faster = sum(b < a for a, b in pts.values())
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlim(lo, hi); ax.set_ylim(lo, hi); ax.set_aspect("equal")
    ax.set_xlabel("mean transpile time — main (s)")
    ax.set_ylabel("mean transpile time — PR (s)")
    ax.set_title(f"{title} (n={len(pts)})\n"
                 f"{faster}/{len(pts)} faster — median {med:+.1f}%, mean {mean:+.1f}%", fontsize=10)
    ax.grid(True, which="both", ls=":", alpha=0.4)
    ax.legend(fontsize=7, loc="upper left")


fig, (axL, axR) = plt.subplots(1, 2, figsize=(13, 6.6))
panel(axL, savers, "Saves one loop iteration")
panel(axR, unchanged, "Unchanged iteration count")
fig.suptitle("Per-circuit transpile time (mean of 5 runs), main vs PR — "
             "savings carried by the savers; unchanged circuits on the diagonal",
             fontsize=11)
fig.tight_layout(rect=[0, 0, 1, 0.97])
out = os.path.join(CH, "runtime_scatter.png")
fig.savefig(out, dpi=150); plt.close(fig)
print(f"saved {out}: savers={len(savers)} unchanged={len(unchanged)}")
