#!/usr/bin/env python3
"""Generate a ratchet manifest: static plateaus of stable concurrency.

Usage: python3 gen_ratchet.py --out manifests/ratchet.json
                  [--levels 1,2,4] [--hold-s 120] [--seed 1]
                  [--reps "r1 r2 r3"] [--instances inst1,inst2]

The schedule is STATIC and deterministic: a discrete-event simulation of
the ratchet (each plateau holds --level sandboxes alive for --hold-s,
replacing a unit the moment its recorded service time ends) assigns every
unit a fixed start_s. Same inputs + seed => byte-identical manifest, so
the same pressure profile reproduces on every host; deviations under load
show up as divergence from this schedule, which is the measurement.

Output schema matches gen_manifest.py exactly, so replay_manifest.py runs
it unchanged. executor_hints gains a "plateaus" list -- the measure-NOW
marks: wall-clock windows (same clock as start_s) in which pressure should
be stable. Executors may ignore the marks or sample during them.

Under high pressure the real run drifts off this schedule (services
stretch); that drift is not compensated for, by design, yet.
"""
import argparse, heapq, json, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_manifest

def ratchet_schedule(pool, levels, hold_s):
    """Simulate the ratchet; returns (units, plateaus).

    `pool` is the shuffled trajectory list (recycled when exhausted).
    Units get fixed start_s values; plateaus record the level windows.
    """
    units, active = [], []          # active: heap of predicted end times
    plateaus = []
    t = 0.0
    i = 0
    def draw():
        nonlocal i
        u = pool[i % len(pool)]
        i += 1
        return u
    for level in levels:
        p_start, p_end = t, t + hold_s
        while len(active) < level:                     # fill at plateau start
            u = draw()
            heapq.heappush(active, t + u["service_s"])
            units.append((t, u))
        while active and active[0] < p_end:            # refill on completion
            t = heapq.heappop(active)
            while len(active) < level:
                u = draw()
                heapq.heappush(active, t + u["service_s"])
                units.append((t, u))
        t = p_end                                      # next plateau starts
        plateaus.append({"level": level,
                         "start_s": round(p_start, 1),
                         "end_s": round(p_end, 1)})
    return units, plateaus

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="output manifest path (.json)")
    ap.add_argument("--levels", default="1,2,4",
                    help="comma-separated concurrency ladder")
    ap.add_argument("--hold-s", type=float, default=120,
                    help="wall-clock seconds per plateau")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--reps", default="r1 r2 r3")
    ap.add_argument("--instances",
                    default=",".join(gen_manifest.INSTANCES),
                    help="comma-separated instance allowlist")
    ap.add_argument("--runs-dir", default=gen_manifest.DEFAULT_RUNS_DIR)
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",")]
    allow = set(args.instances.split(","))
    catalog = [u for u in gen_manifest.discover_units(args.runs_dir,
                                                      args.reps.split())
               if u["instance"] in allow]
    if not catalog:
        sys.exit(f"no trajectories under {args.runs_dir} "
                 f"for reps {args.reps} instances {sorted(allow)}")
    rng = random.Random(args.seed)
    rng.shuffle(catalog)

    scheduled, plateaus = ratchet_schedule(catalog, levels, args.hold_s)
    units = [{
        "id": f"u{i:04d}",
        "start_s": round(start, 3),
        "instance": u["instance"],
        "rep": u["rep"],
        "service_s": round(u["service_s"], 1),
        "expected_end_s": round(start + u["service_s"], 1),
        "files": u["files"],
    } for i, (start, u) in enumerate(sorted(scheduled, key=lambda x: x[0]))]
    drain = max((u["expected_end_s"] for u in units), default=0.0)
    measure_end = plateaus[-1]["end_s"] if plateaus else 0.0

    manifest = {
        "schema": gen_manifest.MANIFEST_SCHEMA,
        "generator": {
            "script": os.path.abspath(__file__),
            "seed": args.seed,
            "levels": levels,
            "hold_s": args.hold_s,
            "reps": args.reps.split(),
            "instances": sorted(allow),
            "pool": len(catalog),
        },
        "executor_hints": {
            "measure_window_s": [0.0, measure_end],
            "drain_by_s": round(drain, 1),
            "unit_schema": gen_manifest.UNIT_SCHEMA,
            "plateaus": plateaus,
            "note": "start each unit at its start_s; replay timed (timeline "
                    "file) so service times hold. 'plateaus' are the "
                    "measure-NOW marks: concurrency should be stable within "
                    "each [start_s, end_s] window; engines may ignore them "
                    "or sample resources during them.",
        },
        "units": units,
    }
    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"wrote {args.out}: {len(units)} units, "
          f"levels {levels} x {args.hold_s:.0f}s, pool {len(catalog)} "
          f"(seed {args.seed})")
    for p in plateaus:
        inside = sum(1 for u in units
                     if p["start_s"] <= u["start_s"] < p["end_s"])
        print(f"  level {p['level']:3}: window [{p['start_s']:7.1f}s, "
              f"{p['end_s']:7.1f}s), {inside} units launched inside")
    print(f"  drain by {drain:.0f}s")

if __name__ == "__main__":
    main()
