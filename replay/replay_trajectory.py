#!/usr/bin/env python3 -I
"""Replay recorded trajectories against a sandbox backend.

Usage: python3 -I replay_trajectory.py <runs_dir> [--backend podman|vm]
                       [--reps r1] [--timed] [--keep] [--dry-run]

The single-run counterpart of replay_manifest.py -- same flags, same help
shape; they differ only in which trajectories they replay. For each
configured instance and rep: start a fresh sandbox on the chosen backend,
check the testbed is pristine, replay every tool step of the trajectory,
compare the resulting git diff with the recorded patch, stop the sandbox,
and report.

Validation criteria (all three must hold for a PASS):
  1. every recorded tool step succeeds again (exit 0; Edit applies exactly once)
  2. final `git diff` in /testbed matches the run's recorded .patch
  3. the testbed was pristine before the replay

Replaces replay_r1.sh (containers) and replay_vm.sh (VMs), which kept the
same criteria; the lifecycle now goes through replay.py's Sandbox classes,
so both backends are driven identically.

Backends:
  podman   sandbox is a container from icr.io/vopo/swebench-<instance>:latest
  vm       sandbox is a VM from ../images/<instance>/vm (resumes from its
           prep state file sub-second; built by images/to_qcow2.sh)

Exit 0 iff every replayed run passed.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # -I strips it; our own module is trusted
import replay

BATCH = "am5-l30-a"
INSTANCES = ["django__django-11133", "django__django-10914",
             "pydata__xarray-4094"]
IMAGE = "icr.io/vopo/swebench-{instance}:latest"
VM_DIR = os.path.join(HERE, "..", "images", "{instance}", "vm")

USAGE = """\
replay_trajectory.py -- replay recorded trajectories against a sandbox backend

  replay_trajectory.py RUNS_DIR [--backend podman|vm] [--reps r1 r2 ...]
                      [--timed] [--keep] [--dry-run]

  RUNS_DIR    where the recorded runs live (trajectory.json / .patch /
              .timeline.txt files, named <batch>.<instance>.<rep>.*)
  --backend   podman (default) or vm
  --reps      which reps to replay (default: r1)
  --timed     fire each step at its recorded time (timeline slides on
              overrun); default runs steps back-to-back, as fast as possible
  --keep      keep the last sandbox alive for inspection (named on stdout)
  --dry-run   list what would run, do nothing

The counterpart driver for load manifests is replay_manifest.py; it takes
the same --backend flag and drives the same sandboxes.
"""

def sandbox_for(backend, instance, name):
    """The instance's sandbox on `backend` (named `name`), or None (with a
    reason printed) if its artifact is missing."""
    if backend == "podman":
        image = IMAGE.format(instance=instance)
        if subprocess_image_missing(image):
            print(f"SKIP {instance}: image {image} not found")
            return None
        return replay.PodmanSandbox(name, image)
    vmdir = os.path.abspath(VM_DIR.format(instance=instance))
    if not os.path.exists(os.path.join(vmdir, f"{instance}.qcow2")):
        print(f"SKIP {instance}: no {vmdir}/{instance}.qcow2 "
              f"(run images/to_qcow2.sh {instance})")
        return None
    return replay.VmSandbox(name, vmdir)

def subprocess_image_missing(image):
    import subprocess
    return subprocess.run(["podman", "image", "inspect", image],
                          capture_output=True).returncode != 0

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return
    runs_dir = args[0]
    def flag(name, default=None):
        return args[args.index(name) + 1] if name in args else default
    backend = flag("--backend", "podman")
    if backend not in replay.SANDBOXES:
        sys.exit(f"unknown backend {backend!r} (want {' or '.join(replay.SANDBOXES)})")
    reps = (flag("--reps", "r1")).split()
    timed = "--timed" in args
    keep = "--keep" in args
    dry = "--dry-run" in args
    logdir = os.path.join(HERE, "logs", f"trajectory-{backend}")
    os.makedirs(logdir, exist_ok=True)

    planned = [(i, r) for i in INSTANCES for r in reps]
    if dry:
        for instance, rep in planned:
            print(f"{instance} {rep}: {backend} sandbox, "
                  f"{'timed' if timed else 'untimed'} replay")
        print(f"{len(planned)} runs on backend {backend}")
        return

    n_pass = n_fail = 0
    for instance, rep in planned:
        traj = os.path.join(runs_dir, f"{BATCH}.{instance}.{rep}.trajectory.json")
        patch = os.path.join(runs_dir, f"{BATCH}.{instance}.{rep}.patch")
        timeline = os.path.join(runs_dir, f"{BATCH}.{instance}.{rep}.timeline.txt")
        if not os.path.exists(traj):
            print(f"SKIP: no {traj}")
            continue
        sb = sandbox_for(backend, instance, f"replay-{instance}-{rep}")
        if sb is None:
            n_fail += 1        # an unusable artifact is a failed run
            continue
        log = os.path.join(logdir, f"{instance}.{rep}.log")
        print(f"=== {instance} {rep} ({backend}) -> {log}")

        sb.start()
        try:
            base_status = replay.git_status_porcelain(sb.target())
            with open(log, "w") as log_fh:
                timeline_arg = timeline if (timed and os.path.exists(timeline)) else None
                steps_ok = replay.replay_steps(traj, sb.target_spec,
                                               timeline_arg, stdout=log_fh) == 0
            got = replay.git_diff_binary(sb.target())
            recorded = open(patch, "rb").read()
            patch_ok = replay.patch_matches(got, recorded)
            with open(os.path.join(logdir, f"{instance}.{rep}.replay.patch"),
                      "wb") as f:
                f.write(got)
        finally:
            if keep:
                print(f"    (sandbox {sb.name} kept alive: "
                      f"{sb.target_spec})")
            else:
                sb.stop()

        if steps_ok and patch_ok and not base_status:
            print(f"PASS {instance} {rep} (all steps ok, patch identical)")
            n_pass += 1
        else:
            if not steps_ok:
                print(f"FAIL {instance} {rep}: some steps failed (see {log})")
            if not patch_ok:
                print(f"FAIL {instance} {rep}: replay patch differs "
                      f"({logdir}/{instance}.{rep}.replay.patch)")
            if base_status:
                print(f"WARN {instance} {rep}: testbed not pristine at "
                      f"start: {base_status[:200]}")
            n_fail += 1

    print(f"\nreplay summary: PASS={n_pass} FAIL={n_fail}")
    sys.exit(1 if n_fail else 0)

if __name__ == "__main__":
    main()
