import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

groups = {}
with open("/tmp/circuit_times.csv") as f:
    for line in f:
        rest, p = line.rstrip("\n").rsplit(",", 1)
        gname, m = rest.rsplit(",", 1)
        g = gname.split(",", 1)[0]
        groups.setdefault(g, []).append((float(m), float(p)))

_order = ["feynman", "device_hamiltonians", "abstract_small", "abstract_medium",
          "abstract_large", "abstract_hamiltonians"]
_cmap = plt.get_cmap("tab10")
colors = {g: _cmap(i % 10) for i, g in enumerate(_order)}
fig, ax = plt.subplots(figsize=(7.5, 7))

allv = [v for pts in groups.values() for xy in pts for v in xy]
lo, hi = min(allv) * 0.7, max(allv) * 1.4
# y = x reference (equal time)
ax.plot([lo, hi], [lo, hi], "k--", lw=1, label="y = x (equal)", zorder=1)

n_faster = n_total = 0
for g, pts in groups.items():
    xs = [a for a, _ in pts]
    ys = [b for _, b in pts]
    n_faster += sum(1 for a, b in pts if b < a)
    n_total += len(pts)
    ax.scatter(xs, ys, s=9, alpha=0.7, color=colors.get(g, "gray"),
               edgecolor="none", label=f"{g} (n={len(pts)})", zorder=3)

ax.set_xscale("log")
ax.set_yscale("log")
ax.set_xlim(lo, hi)
ax.set_ylim(lo, hi)
ax.set_xlabel("transpile time — main (s)")
ax.set_ylabel("transpile time — main + PR (s)")
ax.set_title(f"Per-circuit transpile time: main vs main+PR\n"
             f"{n_faster}/{n_total} circuits below the line (PR faster)")
ax.text(0.97, 0.04, "below line = PR faster", transform=ax.transAxes,
        ha="right", va="bottom", fontsize=9, style="italic", color="dimgray")
ax.grid(True, which="both", ls=":", alpha=0.4)
ax.legend(loc="upper left", framealpha=0.9, fontsize=9)
ax.set_aspect("equal")
fig.tight_layout()
fig.savefig("/Users/hfwen/Downloads/main_vs_pr_scatter.png", dpi=150)
print(f"saved; {n_faster}/{n_total} circuits PR-faster")
