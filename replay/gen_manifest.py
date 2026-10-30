#!/usr/bin/env python3
"""Generate a load-test manifest: a seeded, precomputed arrival schedule of
trajectory replays.

The manifest is pure data: an ordered list of "units", each an arrival time
plus everything needed to replay one trajectory. It knows nothing about how a
unit will be executed (podman, another engine, ...): the executor reads the
manifest and starts each unit at its appointed start_s. That boundary is
deliberate, so a multi-engine backend can consume the same manifests.

Determinism: the manifest is a pure function of (seed, rate, mix, strata,
duration, warmup, rep policy). Same arguments, same manifest, byte for byte.

Load model (see README):
  - arrivals are a Poisson process at rate lambda (inter-arrival ~ Exp(lambda))
  - each unit's service time is the trajectory's recorded turn_ms (timed
    replay makes actual runtime ~= that), read from the trajectory files,
    not hardcoded
  - mean steady-state concurrency is lambda * E[S] (Little's law); --target
    picks lambda from a wanted mean concurrency under the chosen mix
  - with --strata S, the task mix is drawn as a shuffled quota per S-second
    window instead of iid, which pins the composition of concurrently
    resident units much tighter than iid sampling

Usage:
  gen_manifest.py --out load.json [--seed 1] [--rate 0.05 | --target 12]
      [--mix 1,1,1] [--strata 1800] [--duration 7200] [--warmup 1200]
      [--reps r1] [--runs-dir DIR]

Prints a summary including the predicted concurrency profile of the schedule
(what the executor should produce, if service times hold).
"""
import argparse, json, math, os, random, sys

DEFAULT_RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "runs", "container")
BATCH = "am5-l30-a"
INSTANCES = ["django__django-11133", "django__django-10914", "pydata__xarray-4094"]
UNIT_SCHEMA = "autobench-moca/load-manifest/unit/1"
MANIFEST_SCHEMA = "autobench-moca/load-manifest/1"


def discover_units(runs_dir, reps):
    """[(instance, rep, files...)] with service times from the trajectories."""
    units = []
    for inst in INSTANCES:
        for rep in reps:
            traj = os.path.join(runs_dir, f"{BATCH}.{inst}.{rep}.trajectory.json")
            if not os.path.isfile(traj):
                continue
            with open(traj) as f:
                t = json.load(f)
            assert t["hands"] == "local", f"{traj}: not a container-hands run"
            units.append({
                "instance": inst,
                "rep": rep,
                "service_s": t["turn_ms"] / 1000.0,
                "files": {
                    "trajectory": traj,
                    "timeline": traj.replace(".trajectory.json", ".timeline.txt"),
                    "patch": traj.replace(".trajectory.json", ".patch"),
                },
            })
    if not units:
        sys.exit(f"no trajectories found under {runs_dir} for reps {reps}")
    return units


def poisson_arrivals(rng, rate, t_start, t_end):
    """Poisson process arrival times in [t_start, t_end), unsorted per process
    order (already increasing by construction)."""
    t, out = t_start, []
    while True:
        t += rng.expovariate(rate)
        if t >= t_end:
            return out
        out.append(t)


def stratified_categories(rng, arrivals, weights, strata_s):
    """Category per arrival, as shuffled quotas per strata window."""
    k = len(weights)
    total = sum(weights)
    # expected arrivals per window at the actual arrival density; use the
    # observed count per window, allocate quota proportionally, shuffle
    by_window = {}
    for a in arrivals:
        by_window.setdefault(int(a // strata_s), []).append(a)
    cat_of = {}
    for w, ws in sorted(by_window.items()):
        n = len(ws)
        quota = [0] * k
        # largest-remainder allocation of n slots over the weights
        exact = [n * w / total for w in weights]
        for i, e in enumerate(exact):
            quota[i] = int(math.floor(e))
        for i in sorted(range(k), key=lambda i: -(exact[i] - quota[i])):
            while sum(quota) < n:
                quota[i] += 1
                break
        # fix any overshoot (shouldn't happen with largest-remainder)
        while sum(quota) > n:
            quota[quota.index(max(quota))] -= 1
        cats = [c for c in range(k) for _ in range(quota[c])]
        rng.shuffle(cats)
        for a, c in zip(ws, cats):
            cat_of[round(a, 6)] = c
    return cat_of


def predict(sched):
    """Concurrency profile of [(start_s, service_s)] -> [(t0, t1, concurrency)]."""
    events = []
    for start_s, service_s in sched:
        events.append((start_s, 1))
        events.append((start_s + service_s, -1))
    events.sort()
    conc, profile, t_prev = 0, [], None
    for t, d in events:
        if t_prev is not None and t > t_prev:
            profile.append((t_prev, t, conc))
        conc += d
        t_prev = t
    return profile


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output manifest path (.json)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rate", type=float, help="arrival rate lambda (1/s)")
    ap.add_argument("--target", type=float,
                    help="wanted mean steady-state concurrency; sets lambda "
                         "from the mix's E[S] (Little's law)")
    ap.add_argument("--mix", default="1,1,1",
                    help="task weights, light,medium,heavy (iid unless --strata)")
    ap.add_argument("--strata", type=float, default=None,
                    help="stratify the mix into shuffled quotas per this many "
                         "seconds (e.g. 1800); default: iid draws")
    ap.add_argument("--duration", type=float, default=7200,
                    help="total run length in s, warmup included (admissions "
                         "stop at this time; executor drains after)")
    ap.add_argument("--warmup", type=float, default=1200,
                    help="s before steady state; not in the manifest, only "
                         "reported (executor should start measuring after)")
    ap.add_argument("--reps", default="r1",
                    help="comma-separated reps to draw from, e.g. r1,r2,r3")
    ap.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    args = ap.parse_args()

    if bool(args.rate) == bool(args.target):
        sys.exit("give exactly one of --rate or --target")

    reps = [r.strip() for r in args.reps.split(",")]
    weights = [float(w) for w in args.mix.split(",")]
    if len(weights) != len(INSTANCES):
        sys.exit(f"--mix needs {len(INSTANCES)} weights, got {len(weights)}")

    catalog = discover_units(args.runs_dir, reps)
    by_cat = {inst: [u for u in catalog if u["instance"] == inst]
              for inst in INSTANCES}
    for inst, us in by_cat.items():
        if not us:
            sys.exit(f"no trajectories for {inst} with reps {reps}")

    e_s = sum(w * sum(u["service_s"] for u in by_cat[i]) / len(by_cat[i])
              for i, w in zip(INSTANCES, weights)) / sum(weights)
    if args.target:
        rate = args.target / e_s
    else:
        rate = args.rate
    if rate <= 0:
        sys.exit("rate must be > 0")

    rng = random.Random(args.seed)
    arrivals = poisson_arrivals(rng, rate, 0.0, args.duration)

    if args.strata:
        cat_of = stratified_categories(rng, arrivals, weights, args.strata)
        cats = [cat_of[round(a, 6)] for a in arrivals]
    else:
        cats = [rng.choices(range(len(INSTANCES)), weights=weights)[0]
                for _ in arrivals]

    units = []
    for a, c in zip(arrivals, cats):
        inst = INSTANCES[c]
        # draw the rep uniformly from what's on disk for this instance
        u = by_cat[inst][rng.randrange(len(by_cat[inst]))]
        units.append({
            "id": f"u{len(units):04d}",
            "start_s": round(a, 3),
            "instance": inst,
            "rep": u["rep"],
            "service_s": round(u["service_s"], 1),
            "expected_end_s": round(a + u["service_s"], 1),
            "files": u["files"],
        })

    # predicted concurrency over the steady-state window [warmup, duration)
    profile = predict([(u["start_s"], u["service_s"]) for u in units])
    steady = [(t0, t1, c) for t0, t1, c in profile
              if t1 > args.warmup and t0 < args.duration]
    area = sum((t1 - max(t0, args.warmup)) * c
               for t0, t1, c in steady
               if t0 < args.duration)
    span = max(args.duration - args.warmup, 1e-9)
    mean_c = area / span
    peak = max((c for _, _, c in steady), default=0)
    ends = [u["expected_end_s"] for u in units]
    drain = max(ends) if ends else 0.0

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generator": {
            "script": os.path.abspath(__file__),
            "seed": args.seed,
            "rate_per_s": round(rate, 6),
            "mix": weights,
            "strata_s": args.strata,
            "duration_s": args.duration,
            "warmup_s": args.warmup,
            "reps": reps,
            "expected_service_s": {inst: round(
                sum(u["service_s"] for u in by_cat[inst]) / len(by_cat[inst]), 1)
                for inst in INSTANCES},
            "little": {"E_S_s": round(e_s, 1), "lambda_per_s": round(rate, 6),
                       "predicted_mean_concurrency": round(e_s * rate, 2)},
        },
        # hints for the executor; engines may ignore or extend these
        "executor_hints": {
            "measure_window_s": [args.warmup, args.duration],
            "drain_by_s": round(drain, 1),
            "unit_schema": UNIT_SCHEMA,
            "note": "start each unit at its start_s; replay timed (timeline "
                    "file) so service times hold; container-per-unit, "
                    "resource caps and cgroup sampling left to the engine",
        },
        "units": units,
    }

    with open(args.out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    print(f"wrote {args.out}: {len(units)} units")
    print(f"  rate {rate:.4f}/s (one arrival per {1/rate:.1f}s), "
          f"E[S] {e_s:.0f}s -> predicted mean concurrency {e_s*rate:.1f}")
    print(f"  schedule predicts: mean concurrency {mean_c:.1f}, "
          f"peak {peak} in steady window "
          f"[{args.warmup:.0f}s, {args.duration:.0f}s)")
    print(f"  mix (arrival counts): " + ", ".join(
        f"{inst} {sum(1 for u in units if u['instance'] == inst)}"
        for inst in INSTANCES))
    print(f"  last unit ends at {drain:.0f}s (drain after {args.duration:.0f}s)")


if __name__ == "__main__":
    main()
