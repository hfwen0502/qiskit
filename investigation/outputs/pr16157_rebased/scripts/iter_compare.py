import sys
from collections import Counter

mp, pp = sys.argv[1], sys.argv[2]


def load(p):
    d = {}
    for line in open(p):
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 3:
            d[(parts[0], parts[1])] = parts[2]
    return d


M, P = load(mp), load(pp)
common = [k for k in M if k in P]
delta, pairs = Counter(), Counter()
groups = {}
saved = same = worse = err = 0
noimp = []
for k in common:
    mi, pi = M[k], P[k]
    if mi.startswith("ERR") or pi.startswith("ERR"):
        err += 1
        continue
    mi, pi = int(mi), int(pi)
    delta[mi - pi] += 1
    pairs[(mi, pi)] += 1
    g = groups.setdefault(k[0], [0, 0, 0])
    if pi < mi:
        saved += 1; g[0] += 1
    elif pi == mi:
        same += 1; g[1] += 1; noimp.append(k)
    else:
        worse += 1; g[2] += 1; noimp.append(k)

n = saved + same + worse
print(f"circuits compared: {n}   (ERR rows skipped: {err})")
print(f"PR fewer iterations: {saved}/{n}    equal: {same}    PR more: {worse}\n")
print("iteration delta (main - PR):")
for d in sorted(delta, reverse=True):
    print(f"  {d:+d} iteration(s): {delta[d]} circuits")
print("\nmost common (main, PR) iteration pairs:")
for (mi, pi), c in pairs.most_common(8):
    print(f"  main={mi}, PR={pi}: {c} circuits")
print("\nper group  (PR-fewer / equal / PR-more):")
for g in ["feynman", "device_hamiltonians", "abstract_small", "abstract_medium",
          "abstract_large", "abstract_hamiltonians"]:
    if g in groups:
        s = groups[g]
        print(f"  {g:22s} {s[0]} / {s[1]} / {s[2]}")
if noimp:
    print(f"\ncircuits where PR did NOT save an iteration ({len(noimp)}):")
    for k in noimp[:40]:
        print(f"  {k[0]}: {k[1]}  (main={M[k]}, PR={P[k]})")
