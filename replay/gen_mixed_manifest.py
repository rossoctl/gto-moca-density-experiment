#!/usr/bin/env python3
"""Generate a MIXED-WORKLOAD load manifest: SWE-bench + Terminal-Bench
trajectories in one schedule, holding the load50 ladder's invariants.

The pure rungs (load50-2pct/5pct/10pct) each draw from one benchmark; these
manifests mix both so a rung's duty ratio is no longer confounded with its
workload population.

Invariants held (matching the pure rungs):
  - duration 600 s, arrivals are a Poisson process conditioned on the unit
    count (sorted uniform order statistics -- exact composition, no drift)
  - total turn-seconds ~8400 => mean concurrency ~14 (Little's law)
  - pooled duty ratio = sum(tool-call time) / sum(turn time), hit to within
    ~0.02 pp by construction: the unit multiset is solved first (side shares,
    then a greedy swap fix-up), then laid over the arrival times

Method: the SWE-bench turn-share w solves D = w*D_swe + (1-w)*D_tb for the
rung's duty target D, where each side's duty is the pooled duty of its
trajectory pool. The 10% rung targets nominal 10.0% because the pure rung's
9.69% IS the SWE-bench pool duty (any mix moves off it by construction).

TB pools are batch-a (Claude Code hands), resolved=True, offline-reproducible
reps only (see the load50-2pct commit for why other batches can fail).

Usage: gen_mixed_manifest.py --duty {2,5,10} --out FILE [--seed N]
"""
import argparse, json, os, random

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWE_DIR = os.path.join(REPO_ROOT, "runs", "SWE-bench", "container")
TB_DIR = os.path.join(REPO_ROOT, "runs", "TB30", "local")

SWE = ["django__django-11133", "django__django-10914", "pydata__xarray-4094"]
SWE_REPS = ["r1", "r2", "r3"]

POOLS = {
    2: ["regex-log", "constraints-scheduling"],
    5: ["regex-log", "constraints-scheduling", "fix-git",
        "openssl-selfsigned-cert", "multi-source-data-merger"],
    10: ["log-summary-date-ranges", "distribution-search",
         "openssl-selfsigned-cert", "multi-source-data-merger"],
}
TARGET_DUTY = {2: 0.020, 5: 0.050, 10: 0.100}
DEFAULT_SEED = {2: 21, 5: 51, 10: 101}

TOTAL_TURN_S = 8400.0   # ~14 mean concurrency over the 600 s window
DURATION = 600.0
UNIT_SCHEMA = "autobench-moca/load-manifest/unit/1"
MANIFEST_SCHEMA = "autobench-moca/load-manifest/1"


def load_traj(path, extra):
    with open(path) as f:
        d = json.load(f)
    assert d.get("resolved") is not False, f"{path}: not resolved"
    t = {
        "tool_s": sum(s["dur_ms"] for s in d["steps"]
                      if s["kind"] == "tool") / 1000.0,
        "turn_s": d["turn_ms"] / 1000.0,
        "files": {
            "trajectory": os.path.relpath(path, REPO_ROOT),
            "timeline": os.path.relpath(
                path.replace(".trajectory.json", ".timeline.txt"), REPO_ROOT),
        },
    }
    t.update(extra)
    return t


def swe_catalog():
    out = []
    for inst in SWE:
        for rep in SWE_REPS:
            p = os.path.join(SWE_DIR, f"am5-l30-a.{inst}.{rep}.trajectory.json")
            t = load_traj(p, dict(instance=inst, rep=rep, batch="am5-l30-a",
                                  side="swe"))
            t["files"]["patch"] = os.path.relpath(
                p.replace(".trajectory.json", ".patch"), REPO_ROOT)
            out.append(t)
    return out


def tb_catalog(tasks):
    out = []
    for inst in tasks:
        cls = next(c for c in ("short", "medium", "long") if os.path.exists(
            os.path.join(TB_DIR, c, f"tb30-2-a.{inst}.r1.trajectory.json")))
        for rep in ("r1", "r2", "r3"):
            p = os.path.join(TB_DIR, cls, f"tb30-2-a.{inst}.{rep}.trajectory.json")
            if not os.path.exists(p):
                continue
            t = load_traj(p, dict(instance=inst, rep=rep, batch="tb30-2-a",
                                  side="tb", cls=cls, sandbox={
                                      "image": "icr.io/vopo/tb-env-{instance}:latest",
                                      "user": None, "workdir": None, "path": None}))
            out.append(t)
    return out


def pooled_duty(cat):
    return sum(t["tool_s"] for t in cat) / sum(t["turn_s"] for t in cat)


def build_multiset(swe, tb, w):
    """Counts per trajectory: side turn-second shares, even within side.
    Returns (multiset of catalog keys, catalog, tool_s, turn_s)."""
    def alloc(cat, seconds):
        # count-even per trajectory (the pure rungs' draw model: a side's
        # realized duty then equals its one-copy pooled duty). K copies of
        # every trajectory, +1 on the shortest turns to spend the remainder.
        n = len(cat)
        total_turn = sum(t["turn_s"] for t in cat)
        if seconds <= 0:
            return {i: 0 for i in range(n)}
        k = int(seconds // total_turn)
        counts = {i: k for i in range(n)}
        rem = seconds - k * total_turn
        for i in sorted(range(n), key=lambda i: cat[i]["turn_s"]):
            if rem < cat[i]["turn_s"] * 0.5:
                break
            if rem >= cat[i]["turn_s"]:
                counts[i] += 1
                rem -= cat[i]["turn_s"]
        return counts

    counts = {}
    counts.update({("swe", i): n for i, n in alloc(swe, w * TOTAL_TURN_S).items()})
    counts.update({("tb", i): n for i, n in alloc(tb, (1 - w) * TOTAL_TURN_S).items()})
    cat = {("swe", i): t for i, t in enumerate(swe)}
    cat.update({("tb", i): t for i, t in enumerate(tb)})

    multiset = []
    for k, n in counts.items():
        multiset.extend([k] * n)
    tool = sum(cat[k]["tool_s"] for k in multiset)
    turn = sum(cat[k]["turn_s"] for k in multiset)
    return multiset, cat, tool, turn


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duty", type=int, required=True, choices=[2, 5, 10])
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    rung = args.duty
    seed = args.seed if args.seed is not None else DEFAULT_SEED[rung]
    target = TARGET_DUTY[rung]
    rng = random.Random(seed)

    swe = swe_catalog()
    tb = tb_catalog(POOLS[rung])
    d_swe, d_tb = pooled_duty(swe), pooled_duty(tb)
    if not (min(d_swe, d_tb) <= target <= max(d_swe, d_tb)):
        raise SystemExit(f"duty {target:.3f} outside pool range "
                         f"[{min(d_swe, d_tb):.4f}, {max(d_swe, d_tb):.4f}]")
    w = min(max((target - d_tb) / (d_swe - d_tb), 0.05), 0.95)

    # If BOTH pools sit within +-0.5 pp of the target, every split is in
    # band (the 10% rung: SWE pools at 9.69%, its TB pool at 10.07%) --
    # bisecting there chases count granularity. Fix an even split instead.
    both_in_band = (abs(d_swe - target) < 0.005 and abs(d_tb - target) < 0.005)

    # bisect the SWE turn-share w against REALIZED duty (count granularity
    # means the analytic w only lands approximately; realized duty is
    # monotone in w because the SWE pool's duty is on one side of target
    # and the TB pool's on the other)
    lo, hi = 0.02, 0.98
    best = None
    if both_in_band:
        ms, cat, tool, turn = build_multiset(swe, tb, 0.5)
        best = (tool / turn, 0.5, ms, cat, tool, turn)
    for _ in range(0 if both_in_band else 40):
        mid = (lo + hi) / 2
        ms, cat, tool, turn = build_multiset(swe, tb, mid)
        duty = tool / turn if turn else 0.0
        if best is None or abs(duty - target) < abs(best[0] - target):
            best = (duty, mid, ms, cat, tool, turn)
        # duty(w) is monotone; direction depends on which pool is hotter
        if (d_swe - d_tb) * (duty - target) > 0:
            hi = mid
        else:
            lo = mid
    duty, w, multiset, cat, tool, turn = best
    rng.shuffle(multiset)

    # Poisson process conditioned on n: sorted uniform order statistics
    n = len(multiset)
    arrivals = sorted(rng.random() * DURATION for _ in range(n))

    units = []
    for a, k in zip(arrivals, multiset):
        t = cat[k]
        rec = {
            "id": f"u{len(units):04d}",
            "start_s": round(a, 3),
            "instance": t["instance"],
            "rep": t["rep"],
            "batch": t["batch"],
            "service_s": round(t["turn_s"], 1),
            "expected_end_s": round(a + t["turn_s"], 1),
            "files": dict(t["files"]),
        }
        if "sandbox" in t:
            rec["sandbox"] = dict(t["sandbox"])
        units.append(rec)

    from_swe = sum(1 for u in units if u["batch"] == "am5-l30-a")
    swe_turn = sum(cat[k]["turn_s"] for k in multiset if k[0] == "swe")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generator": {
            "script": os.path.relpath(os.path.abspath(__file__), REPO_ROOT),
            "seed": seed,
            "rate_per_s": round(n / DURATION, 4),
            "swe_turn_share": round(w, 3),
            "duty_target": target,
            "duration_s": DURATION,
            "warmup_s": 0.0,
            "reps": ["r1", "r2", "r3"],
            "source": (f"mixed rung: SWE-bench container hands (turn-share "
                       f"{w:.0%}) + TB-30 batch-a local hands "
                       f"({', '.join(POOLS[rung])})"),
        },
        "executor_hints": {
            "measure_window_s": [0.0, DURATION],
            "drain_by_s": round(max(u["expected_end_s"] for u in units), 1),
            "unit_schema": UNIT_SCHEMA,
            "note": ("mixed rung of the load50 duty ladder; same invariants "
                     "as the pure rungs (C~14, 600 s, duty = sum tool / sum "
                     "turn). TB units carry their own sandbox block."),
        },
        "units": units,
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")

    print(f"{args.out}: {n} units, seed {seed}")
    print(f"  SWE-bench: {from_swe} units, {swe_turn:.0f} turn-s "
          f"({100*swe_turn/turn:.0f}%), pool duty {100*d_swe:.2f}%")
    print(f"  TB-30:     {n-from_swe} units, {turn-swe_turn:.0f} turn-s, "
          f"pool duty {100*d_tb:.2f}%  tasks: {', '.join(POOLS[rung])}")
    print(f"  solved SWE turn-share w = {w:.3f}")
    print(f"  realized: duty {100*tool/turn:.2f}% (target {100*target:.2f}%), "
          f"turn-seconds {turn:.0f} (=> C ~ {turn/DURATION:.1f})")


if __name__ == "__main__":
    main()
