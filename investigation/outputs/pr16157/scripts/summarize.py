"""Per-group aggregate summary: gate count, depth, compile time, main vs PR.

Produces one markdown-style table per group (2Q / 1Q / total for both count
and depth, plus wall-clock), excluding circuits flagged as upstream Bug 2
(see scripts/bug2_repro.py). Those circuits have identical 2Q counts
across branches but tie-break rotational decompositions differently — they
are neither regressions nor wins for this PR.

Usage:
    python summarize.py <evidence_root>

where <evidence_root> is a directory containing
    <group>/main.json
    <group>/pr.json
for each of the 6 groups.
"""
import argparse
import json
import sys
from pathlib import Path


GROUPS = [
    "feynman",
    "device_hamiltonians",
    "abstract_small",
    "abstract_medium",
    "abstract_large",
    "abstract_hamiltonians",
]

# Circuits affected by upstream Qiskit Bug 2 (unseeded RNG tie-break in a 1Q
# rotation decomposition). 2Q count identical on both branches; only 1Q gates
# differ, representing equivalent decompositions. Excluded from the summary
# so a one-off main-vs-PR tie-break flip does not distort aggregate numbers.
BUG2_EXCLUSIONS = {
    # name substring match within b["name"]
    "device_hamiltonians":   ["mu_z_prime_enc_gray_dvalues_4-4-4-4"],
    "abstract_hamiltonians": ["JW24-all-to-all"],
}


METRICS = [
    ("output_gate_count_2q",    "2Q gate count"),
    ("output_gate_count_1q",    "1Q gate count"),
    ("output_gate_count_total", "Total gate count"),
    ("output_depth_2q",         "2Q depth"),
    ("output_depth_1q",         "1Q depth"),
    ("output_depth_total",      "Total depth"),
]


def load(path):
    with open(path) as f:
        return json.load(f)


def fmt_int(n):
    return f"{n:,}"


def fmt_delta(m, p):
    if m == 0:
        return "0" if p == 0 else f"+{p}"
    pct = (p - m) / m * 100
    if pct == 0:
        return "**0 (+0.00%)**"
    sign = "+" if p >= m else ""
    return f"{sign}{p - m} ({sign}{pct:.5f}%)"


def summarize(evidence_root):
    root = Path(evidence_root)
    totals_by_group = {}
    grand_total = {m: [0, 0] for m, _ in METRICS}
    grand_time = [0.0, 0.0]
    grand_n = [0, 0]        # kept / excluded

    for g in GROUPS:
        dm = load(root / g / "main.json")
        dp = load(root / g / "pr.json")
        bm = {b["name"]: b for b in dm["benchmarks"]}
        bp = {b["name"]: b for b in dp["benchmarks"]}
        exclusions = BUG2_EXCLUSIONS.get(g, [])
        common = sorted(set(bm) & set(bp))
        kept = [n for n in common if not any(x in n for x in exclusions)]
        excluded = [n for n in common if any(x in n for x in exclusions)]
        grand_n[0] += len(kept)
        grand_n[1] += len(excluded)

        sums = {m: [0, 0] for m, _ in METRICS}
        tm, tp = 0.0, 0.0
        for name in kept:
            em = bm[name].get("extra_info", {})
            ep = bp[name].get("extra_info", {})
            for m, _ in METRICS:
                sums[m][0] += em.get(m, 0) or 0
                sums[m][1] += ep.get(m, 0) or 0
            tm += bm[name]["stats"]["mean"]
            tp += bp[name]["stats"]["mean"]
            for m, _ in METRICS:
                grand_total[m][0] += em.get(m, 0) or 0
                grand_total[m][1] += ep.get(m, 0) or 0
            grand_time[0] += bm[name]["stats"]["mean"]
            grand_time[1] += bp[name]["stats"]["mean"]

        totals_by_group[g] = {
            "sums": sums, "tm": tm, "tp": tp,
            "kept": len(kept), "excluded": excluded,
        }

    # Emit markdown
    lines = []
    for g in GROUPS:
        t = totals_by_group[g]
        lines.append(f"### {g}")
        lines.append("")
        excl_note = ""
        if t["excluded"]:
            short = [e.split("[")[-1].rstrip("]") if "[" in e else e for e in t["excluded"]]
            excl_note = f" (excludes {len(t['excluded'])} Bug 2 circuit{'s' if len(t['excluded']) > 1 else ''}: `{', '.join(short)}`)"
        lines.append(f"{t['kept']} circuits{excl_note}.")
        lines.append("")
        lines.append("| Metric | Main | PR | Δ (PR − Main) |")
        lines.append("|---|---:|---:|---:|")
        for m, label in METRICS:
            mv, pv = t["sums"][m]
            lines.append(f"| {label} | {fmt_int(mv)} | {fmt_int(pv)} | {fmt_delta(mv, pv)} |")
        if t["tm"] > 0:
            wall_delta = (t["tp"] - t["tm"]) / t["tm"] * 100
            lines.append(f"| Wall-clock (sum of means, s) | {t['tm']:.2f} | {t['tp']:.2f} | **{wall_delta:+.1f}%** |")
        lines.append("")

    # Grand total
    lines.append("### Total")
    lines.append("")
    lines.append(f"{grand_n[0]} circuits (excludes {grand_n[1]} Bug 2 circuits across all groups).")
    lines.append("")
    lines.append("| Metric | Main | PR | Δ (PR − Main) |")
    lines.append("|---|---:|---:|---:|")
    for m, label in METRICS:
        mv, pv = grand_total[m]
        lines.append(f"| {label} | {fmt_int(mv)} | {fmt_int(pv)} | {fmt_delta(mv, pv)} |")
    if grand_time[0] > 0:
        wall_delta = (grand_time[1] - grand_time[0]) / grand_time[0] * 100
        lines.append(f"| Wall-clock (sum of means, s) | {grand_time[0]:.2f} | {grand_time[1]:.2f} | **{wall_delta:+.1f}%** |")

    return "\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("evidence_root", nargs="?", default=str(Path(__file__).parent.parent))
    args = p.parse_args()
    print(summarize(args.evidence_root))
