"""Experiment 4: the step microbench -- where the duty multiplier comes from.

One WARM fc sandbox, one trajectory replayed N times in-process (the same
run_replay the step engine uses).  Per repetition we measure, from three
vantage points:

  guest   /proc/stat 'cpu ' busy delta inside the guest, read over the
          vsock agent just before and just after the replay
  vmm     /proc/<firecracker-pid>/stat utime+stime delta (the VMM's own cost)
  client  getrusage(RUSAGE_CHILDREN) delta (the guest-client subprocesses the
          target spawns per step, plus any other children)

against T = the sum of recorded TOOL durations (the duty numerator).  The
campaign multiplier was host-busy / nominal; here it decomposes into
guest-internal work vs VMM vs client machinery.  Control intervals (same
length, no replay) give the idle drift to subtract.

Synthetic arms on the same VM pin the mechanism: a pure CPU spin and an
IO-heavy find+md5 over the testbed tree, each for a fixed wall time.

Usage (ROOT, from the repo root):
  sudo env FC_MAX_VMS=4 python3 -I replay/microbench/stepbench.py \
      --unit django --out /root/mb/out
"""

import argparse, json, os, resource, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # replay/
sys.path.insert(0, HERE)

import fc as fcmod
import replay

UNITS = {
    # heavy: 25 steps, 17.4 s recorded tool time, includes the test suite
    "django": dict(instance="django__django-10914",
                   traj="runs/SWE-bench/container/"
                        "am5-l30-a.django__django-10914.r2.trajectory.json",
                   tl="runs/SWE-bench/container/"
                      "am5-l30-a.django__django-10914.r2.timeline.txt",
                   tb=False),
    # light IO: 5 steps, 3.5 s recorded tool time (file merging)
    "merger": dict(instance="multi-source-data-merger",
                   traj="runs/TB30/local/short/"
                        "tb30-2-a.multi-source-data-merger.r1.trajectory.json",
                   tl="runs/TB30/local/short/"
                      "tb30-2-a.multi-source-data-merger.r1.timeline.txt",
                   tb=True),
}

_CLK_TCK = os.sysconf("SC_CLK_TCK")


def pid_cpu(pid):
    f = open(f"/proc/{pid}/stat").read()
    rest = f[f.rindex(")") + 2:].split()
    return (int(rest[11]) + int(rest[12])) / _CLK_TCK


def guest_busy(vm):
    """guest aggregate busy jiffies->seconds, via the agent (guest USER_HZ
    assumed 100, the host's -- both kernels are stock Ubuntu)."""
    rc, out, err = vm.exec("grep '^cpu ' /proc/stat", 30)
    if rc != 0:
        raise RuntimeError(f"guest /proc/stat read failed: {err}")
    v = [int(x) for x in out.split()[1:]]
    # user nice system idle iowait irq softirq steal -> busy excludes
    # idle (3) and iowait (4)
    return (sum(v[0:3]) + sum(v[5:8])) / _CLK_TCK


def tool_seconds(timeline):
    return sum(d / 1000 for _, d in replay.parse_timeline(timeline))


def measure_interval(vm, fcpid, fn):
    """Run fn() between two measurement points; return the three deltas."""
    g0 = guest_busy(vm)
    p0 = pid_cpu(fcpid)
    c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    wall0 = time.monotonic()
    fn()
    wall = time.monotonic() - wall0
    g1 = guest_busy(vm)
    p1 = pid_cpu(fcpid)
    c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return dict(wall_s=wall, guest_s=g1 - g0, vmm_s=p1 - p0,
                client_s=(c1.ru_utime + c1.ru_stime) -
                         (c0.ru_utime + c0.ru_stime))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", required=True, choices=sorted(UNITS))
    ap.add_argument("--reps", type=int, default=8)
    ap.add_argument("--synth-reps", type=int, default=10)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    u = UNITS[args.unit]
    root = os.path.dirname(os.path.dirname(HERE))

    # per-unit guest shape, as the manifest driver sets it (fc.py defaults
    # are the swebench shape; TB units run as root at the image's /app)
    if u["tb"]:
        wd_file = os.path.join(root, "images", "tb30", u["instance"],
                               "vm", "workdir")
        os.environ["REPLAY_FC_UID"] = "0"
        os.environ["REPLAY_FC_WORKDIR"] = (
            open(wd_file).read().strip() if os.path.isfile(wd_file)
            else "/app")

    sb = fcmod.FcSandbox("mb-" + args.unit, u["instance"])
    sb.start()
    target = fcmod.FcTarget(sb.template, sb.workspace_dir, mode="warm")
    # force the resident VM now so the pid exists for every measurement
    target.run("true", 30)
    vm = target._vm
    fcpid = vm.proc.pid
    traj = json.load(open(os.path.join(root, u["traj"])))
    times = replay.parse_timeline(os.path.join(root, u["tl"]))
    T = tool_seconds(os.path.join(root, u["tl"]))
    makespan = times[-1][0] / 1000 + times[-1][1] / 1000

    rows = []
    out = open(os.path.join(args.out, f"stepbench-{args.unit}.csv"), "w")
    out.write("arm,rep,wall_s,guest_s,vmm_s,client_s,recorded_s\n")

    # --- control: idle drift over a makespan-length interval ---------------
    for i in range(3):
        r = measure_interval(vm, fcpid,
                             lambda: time.sleep(makespan))
        r["arm"] = "control"; r["rep"] = i; r["recorded_s"] = 0
        rows.append(r); out.write(
            f"control,{i},{r['wall_s']:.1f},{r['guest_s']:.3f},"
            f"{r['vmm_s']:.3f},{r['client_s']:.3f},0\n"); out.flush()

    # --- real trajectory ----------------------------------------------------
    for i in range(args.reps):
        r = measure_interval(vm, fcpid,
                             lambda: replay.run_replay(traj, target, times))
        r["arm"] = "traj"; r["rep"] = i; r["recorded_s"] = T
        rows.append(r); out.write(
            f"traj,{i},{r['wall_s']:.1f},{r['guest_s']:.3f},"
            f"{r['vmm_s']:.3f},{r['client_s']:.3f},{T}\n"); out.flush()
        print(f"traj rep {i}: wall={r['wall_s']:.1f}s guest={r['guest_s']:.2f}s "
              f"vmm={r['vmm_s']:.2f}s client={r['client_s']:.2f}s (T={T:.1f}s)",
              flush=True)

    # --- synthetic arms ------------------------------------------------------
    # no python in TB images: CPU arm is md5 over /dev/zero (pure compute),
    # IO arm is md5 over the testbed tree (read + hash).  Synthetic arms are
    # normalized per wall-second in the summary (recorded_s is nominal).
    spin = "dd if=/dev/zero bs=1M count=1500 2>/dev/null | md5sum"
    io = ("find /workspace/testbed /app -type f 2>/dev/null | head -20000 | "
          "xargs -r md5sum 2>/dev/null | wc -l")
    for name, script in (("synth-cpu", spin), ("synth-io", io)):
        for i in range(args.synth_reps):
            r = measure_interval(vm, fcpid,
                                 lambda: target.run(script, 300))
            r["arm"] = name; r["rep"] = i; r["recorded_s"] = r["wall_s"]
            rows.append(r); out.write(
                f"{name},{i},{r['wall_s']:.1f},{r['guest_s']:.3f},"
                f"{r['vmm_s']:.3f},{r['client_s']:.3f},{r['wall_s']:.1f}\n")
            out.flush()
        print(f"{name} done", flush=True)

    out.close()
    target.close()
    sb.stop()

    def avg(arm, k):
        v = [r[k] for r in rows if r["arm"] == arm]
        return sum(v) / len(v)
    ctrl = {k: avg("control", k) / avg("control", "wall_s")
            for k in ("guest_s", "vmm_s", "client_s")}
    print("\n--- summary (per recorded tool-second) ---")
    for arm in ("traj", "synth-cpu", "synth-io"):
        n = len([r for r in rows if r["arm"] == arm])
        wall = avg(arm, "wall_s")
        g = (avg(arm, "guest_s") - ctrl["guest_s"] * wall)
        v = (avg(arm, "vmm_s") - ctrl["vmm_s"] * wall)
        c = (avg(arm, "client_s") - ctrl["client_s"] * wall)
        den = avg(arm, "recorded_s")
        print(f"{arm:9s} n={n} wall={wall:.1f}s  guest/T={g/den:.2f}  "
              f"vmm/T={v/den:.2f}  client/T={c/den:.2f}  "
              f"(guest+vmm+client)/T={(g+v+c)/den:.2f}")


if __name__ == "__main__":
    main()
