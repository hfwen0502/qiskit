#!/usr/bin/env python3
# Regenerate the four main-vs-PR scatter plots from circuit_full.tsv (same dir).
# Each point is one circuit; the dashed line is y = x.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

rows = []
with open("circuit_full.tsv") as f:
    header = f.readline().rstrip("\n").split("\t")
    for line in f:
        rows.append(dict(zip(header, line.rstrip("\n").split("\t"))))

_order = ["feynman", "device_hamiltonians", "abstract_small", "abstract_medium",
          "abstract_large", "abstract_hamiltonians"]
cmap = plt.get_cmap("tab10")
colors = {g: cmap(i % 10) for i, g in enumerate(_order)}

metrics = [
    ("wall_m", "wall_p", "transpile time (s)", "scatter_time.png", "faster", "below line = PR faster"),
    ("g2q_m", "g2q_p", "2Q gate count", "scatter_2q.png", "fewer", "on line = identical"),
    ("gt_m", "gt_p", "total gate count", "scatter_total_gates.png", "fewer", "on line = identical"),
    ("d_m", "d_p", "total depth", "scatter_depth.png", "smaller", "on line = identical"),
]

for mcol, pcol, label, fname, better, note in metrics:
    pts = {}
    below = equal = above = 0
    for r in rows:
        try:
            x, y = float(r[mcol]), float(r[pcol])
        except (ValueError, TypeError):
            continue
        if x <= 0 or y <= 0:
            continue
        pts.setdefault(r["group"], []).append((x, y))
        below += y < x
        equal += y == x
        above += y > x
    n = below + equal + above
    fig, ax = plt.subplots(figsize=(7, 7))
    allv = [v for g in pts.values() for xy in g for v in xy]
    lo, hi = min(allv) * 0.7, max(allv) * 1.4
    ax.plot([lo, hi], [lo, hi], "k--", lw=1, label="y = x (equal)", zorder=1)
    for g in _order:
        if g not in pts:
            continue
        ax.scatter([a for a, _ in pts[g]], [b for _, b in pts[g]], s=9, alpha=0.7,
                   color=colors[g], edgecolor="none", label=f"{g} (n={len(pts[g])})", zorder=3)
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.set_xlim(lo, hi); ax.set_ylim(lo, hi); ax.set_aspect("equal")
    ax.set_xlabel(f"{label} - main"); ax.set_ylabel(f"{label} - main + PR")
    ax.set_title(f"Per-circuit {label}: main vs main+PR\n"
                 f"{below} {better} / {equal} equal / {above} worse  (n={n})")
    ax.text(0.97, 0.04, note, transform=ax.transAxes, ha="right", va="bottom",
            fontsize=9, style="italic", color="dimgray")
    ax.grid(True, which="both", ls=":", alpha=0.4)
    ax.legend(loc="upper left", framealpha=0.9, fontsize=8)
    fig.tight_layout()
    fig.savefig(fname, dpi=150)
    plt.close(fig)
    print(f"{fname}: {below} {better} / {equal} equal / {above} worse  (n={n})")
