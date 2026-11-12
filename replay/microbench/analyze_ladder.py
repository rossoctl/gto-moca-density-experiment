"""Analyze ladder CSVs: per-rung mean busy/iowait/pid CPU during the
-measure window, plus the busy-vs-K slope (the fixed per-VM tax).

Usage: python3 analyze_ladder.py ladder-fc-run.csv [ladder-qemu-run.csv ...]
"""

import csv, sys

def analyze(path):
    rows = [r for r in csv.DictReader(open(path))]
    by_rung = {}
    for r in rows:
        tag = r["tag"]
        if "-measure" not in tag:
            continue
        K = int(tag.split("K")[1].split("-")[0])
        by_rung.setdefault(K, []).append(
            (float(r["epoch"]), float(r["busy_cores"]),
             float(r["iowait_cores"]), float(r["pid_cpu_cores"])))
    variant = path.split("ladder-")[1].split(".csv")[0]
    print(f"\n== {variant} ==")
    print(f"{'K':>4s} {'n':>4s} {'busy':>7s} {'iowait':>7s} {'pidrate':>7s}")
    ks = sorted(by_rung)
    pts = []
    for K in ks:
        v = by_rung[K]
        n = len(v)
        m = [sum(x[i] for x in v) / n for i in (1, 2)]
        # pid_cpu is a cumulative sum across tracked pids: rate = delta
        # across the measure window / window seconds
        dt = v[-1][0] - v[0][0]
        pidrate = (v[-1][3] - v[0][3]) / dt if dt > 0 else 0.0
        pts.append((K, m[0]))
        print(f"{K:>4d} {n:>4d} {m[0]:7.2f} {m[1]:7.2f} {pidrate:7.2f}")
    if len(pts) >= 2:
        # least-squares slope of busy vs K (through all rungs incl. K=0)
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        n = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in pts)
        slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
        intercept = (sy - slope * sx) / n
        print(f"slope: {slope*1000:.1f} mc/VM (intercept {intercept:.2f} cores)")

for p in sys.argv[1:]:
    analyze(p)
