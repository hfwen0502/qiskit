#!/usr/bin/env python3
"""Build the PR figures from the per-circuit data and print the headline stats.

Inputs (same dir, or paths via argv):
  overnight_periter.tsv : group name iter m_wall p_wall m_2q p_2q m_1q p_1q m_depth p_depth  (5 rows/circuit)
  iters_main_full.tsv / iters_pr_full.tsv : group name iters

Outputs PNGs into ./charts and prints stats (savers %, mean reduction among savers,
unchanged-iteration Δ, per-group breakdown).
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


# --- load -----------------------------------------------------------------
peri = os.path.join(BASE, "overnight_periter.tsv")
rows = []
with open(peri) as f:
    hdr = f.readline().rstrip("\n").split("\t")
    for ln in f:
        rows.append(dict(zip(hdr, ln.rstrip("\n").split("\t"))))
M_it = load_iters(os.path.join(BASE, "iters_main_full.tsv"))
P_it = load_iters(os.path.join(BASE, "iters_pr_full.tsv"))

# per-circuit aggregation across the 5 iters
circ = defaultdict(lambda: {"mw": [], "pw": [], "2q": [], "1q": [], "dp": []})
group_iter_wall = defaultdict(lambda: {"m": defaultdict(float), "p": defaultdict(float)})
for r in rows:
    g, key = r["group"], (r["group"], norm(r["name"]))
    c = circ[key]
    c["mw"].append(float(r["m_wall"])); c["pw"].append(float(r["p_wall"]))
    c["2q"].append((int(r["m_2q"]), int(r["p_2q"])))
    c["1q"].append((int(r["m_1q"]), int(r["p_1q"])))
    c["dp"].append((int(r["m_depth"]), int(r["p_depth"])))
    group_iter_wall[g]["m"][r["iter"]] += float(r["m_wall"])
    group_iter_wall[g]["p"][r["iter"]] += float(r["p_wall"])

# === 1. iteration histogram ===============================================
mi_vals = list(M_it.values())
pi_vals = [P_it[k] for k in M_it if k in P_it]
mi_paired = [M_it[k] for k in M_it if k in P_it]
lo, hi = min(mi_paired + pi_vals), max(mi_paired + pi_vals)
bins = range(lo, hi + 2)
fig, ax = plt.subplots(figsize=(8, 5))
import numpy as np
xs = np.arange(lo, hi + 1)
mc = [mi_paired.count(x) for x in xs]
pc = [pi_vals.count(x) for x in xs]
w = 0.4
ax.bar(xs - w / 2, mc, w, label="main", color="#888")
ax.bar(xs + w / 2, pc, w, label="PR (signal exit)", color="#1f77b4")
ax.set_xlabel("optimization-loop iterations"); ax.set_ylabel("number of circuits")
saved = sum(1 for k in M_it if k in P_it and P_it[k] < M_it[k])
tot = sum(1 for k in M_it if k in P_it)
ax.set_title(f"Level-2 optimization-loop iterations (n={tot})\n"
             f"{saved} circuits ({100*saved/tot:.0f}%) run one fewer with the PR; none run more")
ax.set_xticks(list(xs)); ax.legend(); ax.grid(axis="y", ls=":", alpha=0.4)
fig.tight_layout(); fig.savefig(f"{CH}/iteration_histogram.png", dpi=150); plt.close(fig)

# === 2. runtime scatter (per-circuit mean) ================================
fig, ax = plt.subplots(figsize=(7, 7))
allv = []
faster = 0; ncirc = 0
for (g, _), c in circ.items():
    mm, pm = statistics.fmean(c["mw"]), statistics.fmean(c["pw"])
    allv += [mm, pm]; ncirc += 1; faster += pm < mm
lo2, hi2 = min(allv) * 0.7, max(allv) * 1.4
ax.plot([lo2, hi2], [lo2, hi2], "k--", lw=1, label="y = x", zorder=1)
for g in GROUPS:
    pts = [(statistics.fmean(c["mw"]), statistics.fmean(c["pw"]))
           for (gg, _), c in circ.items() if gg == g]
    if pts:
        ax.scatter([a for a, _ in pts], [b for _, b in pts], s=9, alpha=0.7,
                   color=COLOR[g], edgecolor="none", label=g)
ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlim(lo2, hi2); ax.set_ylim(lo2, hi2)
ax.set_aspect("equal"); ax.set_xlabel("mean transpile time — main (s)")
ax.set_ylabel("mean transpile time — PR (s)")
ax.set_title(f"Per-circuit transpile time (mean of 5 runs)\n{faster}/{ncirc} below the line (PR faster)")
ax.grid(True, which="both", ls=":", alpha=0.4); ax.legend(fontsize=8, loc="upper left")
fig.tight_layout(); fig.savefig(f"{CH}/runtime_scatter.png", dpi=150); plt.close(fig)

# === 3. per-group avg ± stdev bars ========================================
fig, ax = plt.subplots(figsize=(9, 5))
x = np.arange(len(GROUPS)); w = 0.38
mmean = [statistics.fmean(group_iter_wall[g]["m"].values()) for g in GROUPS]
mstd = [statistics.pstdev(group_iter_wall[g]["m"].values()) for g in GROUPS]
pmean = [statistics.fmean(group_iter_wall[g]["p"].values()) for g in GROUPS]
pstd = [statistics.pstdev(group_iter_wall[g]["p"].values()) for g in GROUPS]
ax.bar(x - w / 2, mmean, w, yerr=mstd, capsize=3, label="main", color="#888")
ax.bar(x + w / 2, pmean, w, yerr=pstd, capsize=3, label="PR", color="#1f77b4")
for i, g in enumerate(GROUPS):
    ax.text(i, max(mmean[i], pmean[i]) * 1.05, f"{(pmean[i]-mmean[i])/mmean[i]*100:+.1f}%",
            ha="center", fontsize=8)
ax.set_yscale("log"); ax.set_xticks(x)
ax.set_xticklabels([g.replace("_", "\n") for g in GROUPS], fontsize=8)
ax.set_ylabel("group total transpile time (s, log)")
ax.set_title("Per-group transpile time, mean ± stdev over 5 runs")
ax.legend(); ax.grid(axis="y", which="both", ls=":", alpha=0.4)
fig.tight_layout(); fig.savefig(f"{CH}/runtime_by_group.png", dpi=150); plt.close(fig)

# === 4. gate/depth all-point scatters =====================================
def scatter_metric(keyname, label, fname, highlight=False):
    fig, ax = plt.subplots(figsize=(7, 7))
    allp = []
    offpts = []  # (a, b, circuit_id) for points off the diagonal
    for (g, cid), c in circ.items():
        for a, b in c[keyname]:
            if a > 0 and b > 0:
                allp.append((g, a, b))
                if a != b:
                    offpts.append((a, b, cid))
    lo, hi = min(min(a, b) for _, a, b in allp) * 0.7, max(max(a, b) for _, a, b in allp) * 1.4
    ax.plot([lo, hi], [lo, hi], "k--", lw=1, label="y = x", zorder=1)
    for g in GROUPS:
        gp = [(a, b) for gg, a, b in allp if gg == g]
        if gp:
            ax.scatter([a for a, _ in gp], [b for _, b in gp], s=6, alpha=0.5,
                       color=COLOR[g], edgecolor="none", label=g)
    if highlight and offpts:
        ax.scatter([a for a, _, _ in offpts], [b for _, b, _ in offpts], s=70,
                   facecolors="none", edgecolors="red", linewidths=1.3, zorder=6,
                   label="off-diagonal (1Q non-determinism)")
        names = sorted(set(cid for _, _, cid in offpts))
        ax.text(0.03, 0.97, "off-diagonal circuits:\n" + "\n".join(names),
                transform=ax.transAxes, va="top", ha="left", fontsize=7, color="red")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
    ax.set_aspect("equal"); ax.set_xlabel(f"{label} — main"); ax.set_ylabel(f"{label} — PR")
    ax.set_title(f"Per-circuit {label} (all 5 runs)\n{len(allp)-len(offpts)}/{len(allp)} points identical")
    ax.grid(True, which="both", ls=":", alpha=0.4); ax.legend(fontsize=8, loc="lower right")
    fig.tight_layout(); fig.savefig(f"{CH}/{fname}", dpi=150); plt.close(fig)
    return len(offpts)

off2q = scatter_metric("2q", "2Q gate count", "scatter_2q.png")
off1q = scatter_metric("1q", "1Q gate count", "scatter_1q.png", highlight=True)
offdp = scatter_metric("dp", "circuit depth", "scatter_depth.png")

# === stats for the draft ==================================================
savers_red, unch_red = [], []
pg = {g: {"save": 0, "tot": 0} for g in GROUPS}
for key, c in circ.items():
    g = key[0]
    if key not in M_it or key not in P_it:
        continue
    mm, pm = statistics.fmean(c["mw"]), statistics.fmean(c["pw"])
    red = (pm - mm) / mm * 100
    pg[g]["tot"] += 1
    if P_it[key] < M_it[key]:
        savers_red.append(red); pg[g]["save"] += 1
    elif P_it[key] == M_it[key]:
        unch_red.append(red)

print("=== STATS FOR DRAFT ===")
print(f"savers: {saved}/{tot} = {100*saved/tot:.1f}%")
print(f"mean runtime reduction among savers (Y%):  {statistics.fmean(savers_red):+.1f}%  "
      f"(median {statistics.median(savers_red):+.1f}%)")
print(f"mean runtime delta on unchanged-iteration circuits: {statistics.fmean(unch_red):+.2f}%")
print(f"off-diagonal points -> 2Q:{off2q}  1Q:{off1q}  depth:{offdp}")
print("per-group savers:", {g: f"{pg[g]['save']}/{pg[g]['tot']}" for g in GROUPS})
print(f"charts written to {CH}")
