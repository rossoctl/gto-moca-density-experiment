#!/usr/bin/env python3
"""EC2 on-demand price vs RAM, at constant vCPU counts (us-east-1, 2026-11-01).

Reads /tmp/ec2data/ec2_priced.csv (instance,vcpu,ram_gb,price_hr) produced from
the AWS bulk pricing API. Emits ec2-cost-vs-ram.dat + ec2-cost-vs-ram.png.
"""
import csv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CSV = "/tmp/ec2data/ec2_priced.csv"
VCPU_CLASSES = [8, 16, 32]  # scatter form: 3-series cap (all-pairs color floors)

# validated palette, slots 1-3 (see dataviz reference; aqua carries the
# contrast WARN -> mitigated by direct labels + legend)
SERIES = {8: "#2a78d6", 16: "#eb6834", 32: "#1baf7a"}
SURFACE, GRID, INK, MUTED = "#fcfcfb", "#e1e0d9", "#0b0b0b", "#52514e"

rows = []
with open(CSV) as f:
    for r in csv.DictReader(f):
        rows.append((r["instance"], int(r["vcpu"]), float(r["ram_gb"]),
                     float(r["price_hr"])))

fig, ax = plt.subplots(figsize=(8.6, 5.4), dpi=160)
fig.patch.set_facecolor(SURFACE)
ax.set_facecolor(SURFACE)

dat = open("ec2-cost-vs-ram.dat", "w")
dat.write("# instance\tvcpu\tram_gb\tprice_hr\n")
for n in VCPU_CLASSES:
    pts = sorted((r for r in rows if r[1] == n), key=lambda r: r[2])
    if not pts:
        continue
    xs = [p[2] for p in pts]
    ys = [p[3] for p in pts]
    c = SERIES[n]
    ax.scatter(xs, ys, s=26, color=c, zorder=3, linewidth=0)
    # power-law trendline through the class
    lx = [__import__("math").log(x) for x in xs]
    ly = [__import__("math").log(y) for y in ys]
    k = len(xs)
    mx, my = sum(lx) / k, sum(ly) / k
    b = sum((a - mx) * (e - my) for a, e in zip(lx, ly)) / \
        sum((a - mx) ** 2 for a in lx)
    a0 = __import__("math").exp(my - b * mx)
    tx = [min(xs) * 0.9, max(xs) * 1.1]
    ax.plot(tx, [a0 * t ** b for t in tx], color=c, lw=1.4, alpha=0.55,
            zorder=2, linestyle="-")
    # direct label at the right end of the trendline
    ax.annotate(f"{n} vCPU  (RAM^{{{b:+.2f}}})",
                xy=(tx[1], a0 * tx[1] ** b), xytext=(4, 2),
                textcoords="offset points", fontsize=9, color=INK,
                fontweight="bold")
    for p in pts:
        dat.write(f"{p[0]}\t{p[1]}\t{p[2]}\t{p[3]}\n")
dat.close()

# selective direct labels for the RAM-ratio extremes of each class
for n, side in ((8, 0), (16, 0), (32, 0)):
    pts = sorted((r for r in rows if r[1] == n), key=lambda r: r[2])
    for p in (pts[0], pts[-1]):
        ax.annotate(p[0], xy=(p[2], p[3]), xytext=(4, 4),
                    textcoords="offset points", fontsize=7, color=MUTED)

ax.set_xscale("log", base=2)
ax.set_yscale("log")
ax.set_xlabel("RAM (GB)", color=INK)
ax.set_ylabel("On-demand price (USD/hr)", color=INK)
ax.set_title("EC2 on-demand cost vs RAM at constant vCPU — us-east-1, Linux",
             color=INK, fontsize=12, loc="left")
ax.grid(True, which="major", color=GRID, lw=0.7)
ax.grid(True, which="minor", color=GRID, lw=0.35, alpha=0.6)
ax.set_axisbelow(True)
for s in ax.spines.values():
    s.set_color(GRID)
ax.tick_params(colors=MUTED, labelsize=8)

# legend (identity never color-alone)
handles = [plt.Line2D([], [], marker="o", ls="", color=SERIES[n],
                      label=f"{n} vCPU") for n in VCPU_CLASSES]
ax.legend(handles=handles, frameon=False, fontsize=9, loc="upper left")

fig.text(0.99, 0.01, "920 on-demand Linux types, shared tenancy; faint line = power-law fit per class",
         ha="right", fontsize=7, color=MUTED)
fig.tight_layout()
fig.savefig("ec2-cost-vs-ram.png", facecolor=SURFACE)
print("wrote ec2-cost-vs-ram.png / .dat")
