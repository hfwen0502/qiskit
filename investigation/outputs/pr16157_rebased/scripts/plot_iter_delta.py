#!/usr/bin/env python3
"""Per-circuit iteration-delta scatter: x = circuit (grouped), y = PR - main iterations,
color = group. Replaces the histogram."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

GROUPS = ["feynman", "device_hamiltonians", "abstract_small", "abstract_medium",
          "abstract_large", "abstract_hamiltonians"]
CMAP = plt.get_cmap("tab10")
COLOR = {g: CMAP(i) for i, g in enumerate(GROUPS)}


def load(p):
    d = {}
    for ln in open(p):
        a = ln.rstrip("\n").split("\t")
        if len(a) == 3 and not a[2].startswith("ERR"):
            d[(a[0], a[1])] = int(a[2])
    return d


M = load("/tmp/iters_main_full.tsv")
P = load("/tmp/iters_pr_full.tsv")
keys = [k for k in M if k in P]
# order circuits by group, then name -> contiguous x-blocks per group
keys.sort(key=lambda k: (GROUPS.index(k[0]) if k[0] in GROUPS else 99, k[1]))

rng = np.random.RandomState(0)
fig, ax = plt.subplots(figsize=(11, 4.5))
group_centers = {}
for x, k in enumerate(keys):
    g = k[0]
    group_centers.setdefault(g, []).append(x)
saved = 0
for x, k in enumerate(keys):
    g = k[0]
    delta = P[k] - M[k]
    saved += delta < 0
    ax.scatter(x, delta + rng.uniform(-0.13, 0.13), s=9, alpha=0.5,
               color=COLOR.get(g, "gray"), edgecolor="none")
# group separators + centered labels
xticks, xlabels = [], []
running = 0
for g in GROUPS:
    xs = group_centers.get(g, [])
    if not xs:
        continue
    xticks.append(sum(xs) / len(xs))
    # stagger every other label down a line so narrow neighbors don't overlap
    prefix = "\n" if len(xlabels) % 2 == 1 else ""
    xlabels.append(f"{prefix}{g}\n(n={len(xs)})")
    running = max(xs)
    ax.axvline(running + 0.5, color="0.85", lw=0.7, zorder=0)
ax.set_yticks([0, -1])
ax.set_yticklabels(["0\n(same)", "−1\n(one fewer)"])
ax.set_ylim(-1.6, 0.6)
ax.set_xticks(xticks)
ax.set_xticklabels(xlabels, fontsize=8)
ax.set_ylabel("PR − main\n(loop iterations)")
n = len(keys)
ax.set_title(f"Per-circuit change in Level-2 loop iterations (PR − main), n={n}\n"
             f"{saved} circuits run one fewer ({100*saved/n:.0f}%); {n-saved} unchanged; none more")
# legend by group
handles = [plt.Line2D([0], [0], marker="o", ls="", color=COLOR[g], label=g) for g in GROUPS]
ax.legend(handles=handles, fontsize=7, loc="lower right", ncol=2, framealpha=0.9)
ax.grid(axis="y", ls=":", alpha=0.4)
fig.tight_layout()
fig.savefig("/tmp/prdir/charts/iteration_delta.png", dpi=150)
print(f"saved iteration_delta.png — {saved}/{n} circuits -1")
