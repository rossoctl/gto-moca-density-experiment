#!/usr/bin/env python3
"""Execute a load manifest: start each unit's sandbox at its appointed
start_s and run the timed replay of its trajectory in it.

Usage: python3 -I replay_manifest.py <manifest.json> [--dry-run] [--keep]
                       [--backend podman|vm|moca] (default podman)
                       [--max-live N]    cap concurrent live sandboxes

The same manifest runs unchanged on either backend. One thread per unit
(the point is load, so admission is never delayed by running units; the
manifest's start_s is law). Each unit:
  1. start its sandbox via replay.py's Sandbox classes (a podman container,
     or a VM resumed from images/<instance>/vm's prep state)
  2. replay.py <trajectory> <target> <timeline>      (timed)
  3. compare git diff in /testbed with the unit's recorded patch
  4. stop the sandbox (unless --keep)

VM notes: each unit gets its own VM (own console socket, writes on a
throwaway overlay), resumed from the prep state file when present.
Concurrent VMs each hold their full -m allocation.

Per-unit logs and a per-unit result line go to logs/manifest-<name>-<backend>/.
Exit 0 iff every unit replayed with an identical patch.
"""
import json, os, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # -I strips it; our own module is trusted
import replay

REPO_ROOT = os.path.dirname(HERE)

def _resolve(path):
    """Manifests are host-independent: file paths are repo-relative
    (absolute paths, from older manifests, pass through unchanged)."""
    return path if os.path.isabs(path) else os.path.normpath(
        os.path.join(REPO_ROOT, path))

IMAGE = "icr.io/vopo/swebench-{instance}:latest"
VM_DIR = os.path.join(HERE, "..", "images", "{instance}", "vm")

class Progress:
    """Thread-safe tally of launched/finished units, with a status printer.

    The ETA is the manifest's own schedule: the latest expected_end_s among
    units not yet finished, minus elapsed. Units that slid (overrun under
    load) make it an underestimate; finished units use their actual end, so
    the estimate self-corrects as units land.
    """
    def __init__(self, units, interval=10.0):
        self.lock = threading.Lock()
        self.interval = interval
        self.total = len(units)
        self.launched = 0
        self.finished = 0
        self.ok = 0
        self.failed = []
        # expected end (schedule time) of every unit; popped as units finish
        self.pending_ends = {u["id"]: u["expected_end_s"] for u in units}
        self.stop = threading.Event()

    def unit_launched(self):
        with self.lock:
            self.launched += 1

    def unit_finished(self, uid, status, end_s):
        with self.lock:
            self.finished += 1
            if status == "ok":
                self.ok += 1
            else:
                self.failed.append((uid, status))
            self.pending_ends.pop(uid, None)
            # units still out: at least until their expected end, or now if slid
            last = max(self.pending_ends.values(), default=0.0)
            return max(last, end_s)

    def line(self, elapsed):
        with self.lock:
            active = self.launched - self.finished
            last = max(self.pending_ends.values(), default=0.0)
            eta = max(0.0, last - elapsed)
            fails = f", {len(self.failed)} failed" if self.failed else ""
            return (f"[{elapsed:7.0f}s] launched {self.launched}/{self.total}, "
                    f"active {active}, finished {self.finished} "
                    f"({self.ok} ok{fails}), ETA {eta/60:5.1f} min")

    def monitor(self, t0):
        while not self.stop.wait(self.interval):
            print(self.line(time.monotonic() - t0), flush=True)

class StartError(Exception):
    pass

def sandbox_for(u, backend):
    """The unit's sandbox, via replay.py's Sandbox classes (the drivers never
    see how a backend is started or stopped)."""
    name = f"load-{u['id']}"
    if backend == "podman":
        return replay.PodmanSandbox(name, IMAGE.format(instance=u["instance"]))
    if backend == "moca":
        return replay.MocaSandbox(name, u["instance"])
    vmdir = os.path.abspath(VM_DIR.format(instance=u["instance"]))
    if not os.path.exists(os.path.join(vmdir, f"{u['instance']}.qcow2")):
        raise StartError(f"no {vmdir}/{u['instance']}.qcow2 "
                         f"(run images/to_qcow2.sh {u['instance']})")
    return replay.VmSandbox(name, vmdir)

class LiveGate:
    """Caps concurrent live sandboxes (--max-live N), backend-agnostic: a
    slot is held from sandbox start to sandbox stop. The static schedule
    stays law for when units are released; the cap only prevents overshoot
    (completions lag under load, and an uncapped schedule stacks
    replacements on still-running sandboxes -- on the VM backend that
    overshoot is what turned level 100 into a 245-VM OOM spiral)."""
    def __init__(self, max_live):
        self.max_live = max_live
        self.cv = threading.Condition()
        self.live = 0

    def acquire(self):
        if not self.max_live:
            return
        with self.cv:
            while self.live >= self.max_live:
                self.cv.wait()
            self.live += 1

    def release(self):
        if not self.max_live:
            return
        with self.cv:
            self.live -= 1
            self.cv.notify()

def run_unit(u, backend, logdir, keep, results, progress, sched_t0, gate):
    log = open(os.path.join(logdir, f"{u['id']}.log"), "w")
    def say(msg):
        log.write(msg + "\n"); log.flush()
    start = time.monotonic()
    sb = None
    holds_slot = False
    status = "crashed"  # replaced below unless something raises first
    try:
        say(f"{u['id']} {u['instance']} {u['rep']} start_s={u['start_s']} "
            f"backend={backend}")
        progress.unit_launched()
        gate.acquire()
        holds_slot = True
        try:
            sb = sandbox_for(u, backend)
            sb.start()
        except (Exception, SystemExit) as e:     # SystemExit: kvm preflight
            say(f"start failed: {e}")
            status = "start-fail"
            return

        steps_ok = (replay.replay_steps(u["files"]["trajectory"],
                                        sb.target_spec,
                                        u["files"]["timeline"],
                                        stdout=log) == 0)

        # end-state check: git diff vs the recorded patch
        d = replay.git_diff_binary(sb.target())
        open(os.path.join(logdir, f"{u['id']}.replay.patch"), "wb").write(d)
        rec = open(u["files"]["patch"], "rb").read()
        patch_ok = replay.patch_matches(d, rec)
        status = "ok" if (steps_ok and patch_ok) else \
                 f"fail(steps_ok={steps_ok},patch_ok={patch_ok})"
        say(f"done in {time.monotonic()-start:.1f}s "
            f"(expected {u['service_s']}s): "
            f"{'ok' if status == 'ok' else 'FAIL'}")
    except Exception as e:                       # keep the manifest running
        say(f"unit crashed: {e!r}")
    finally:
        # finish in "schedule time": the unit's start_s plus how long it ran
        end_s = u["start_s"] + (time.monotonic() - start)
        results.append((u["id"], status))
        progress.unit_finished(u["id"], status, end_s)
        print(progress.line(time.monotonic() - sched_t0), flush=True)
        if sb and not keep:
            try:
                sb.stop()
            except Exception as e:
                say(f"sandbox stop failed: {e!r}")
        elif sb:
            say(f"sandbox kept alive: {sb.target_spec}")
        if holds_slot:
            gate.release()
        log.close()

def main():
    manifest_path = sys.argv[1]
    dry = "--dry-run" in sys.argv
    keep = "--keep" in sys.argv
    backend = "podman"
    if "--backend" in sys.argv:
        backend = sys.argv[sys.argv.index("--backend") + 1]
    if backend not in ("podman", "vm", "moca"):
        sys.exit(f"unknown backend {backend!r} (want {' or '.join(replay.SANDBOXES)})")
    max_live = None
    if "--max-live" in sys.argv:
        max_live = int(sys.argv[sys.argv.index("--max-live") + 1])
    gate = LiveGate(max_live)
    m = json.load(open(manifest_path))
    units = sorted(m["units"], key=lambda u: u["start_s"])
    for u in units:
        u["files"] = {k: _resolve(v) for k, v in u["files"].items()}
    logdir = os.path.join(HERE, "logs",
                          "manifest-" + os.path.basename(manifest_path).rsplit(".", 1)[0]
                          + "-" + backend)
    os.makedirs(logdir, exist_ok=True)

    if dry:
        for u in units:
            print(f"{u['start_s']:9.1f}s  {u['id']}  {u['instance']:22} {u['rep']}"
                  f"  service {u['service_s']}s")
        print(f"backend {backend}")
        for p in m["executor_hints"].get("plateaus", []):
            inside = sum(1 for u in units
                         if p["start_s"] <= u["start_s"] < p["end_s"])
            print(f"plateau level {p['level']}: [{p['start_s']:.1f}s, "
                  f"{p['end_s']:.1f}s), {inside} units inside")
        print(f"{len(units)} units, measure window "
              f"{m['executor_hints']['measure_window_s']}, "
              f"drain by {m['executor_hints']['drain_by_s']}s"
              + (f", max-live {max_live}" if max_live else ""))
        return

    t0 = time.monotonic()
    progress = Progress(units)
    mon = threading.Thread(target=progress.monitor, args=(t0,), daemon=True)
    mon.start()
    threads, results = [], []
    for u in units:
        wait = u["start_s"] - (time.monotonic() - t0)
        if wait > 0:
            time.sleep(wait)
        print(f"{progress.line(time.monotonic() - t0)} | "
              f"launching {u['id']} {u['instance']} {u['rep']}", flush=True)
        th = threading.Thread(target=run_unit,
                              args=(u, backend, logdir, keep, results, progress,
                                    t0, gate))
        th.start(); threads.append(th)
    for th in threads:
        th.join()
    progress.stop.set()

    n_ok = sum(1 for _, s in results if s == "ok")
    print(f"\nmanifest done in {time.monotonic()-t0:.0f}s: "
          f"{n_ok}/{len(results)} units ok")
    for uid, s in sorted(results):
        if s != "ok":
            print(f"  {uid}: {s} (see {logdir}/{uid}.log)")
    sys.exit(0 if n_ok == len(results) else 1)

if __name__ == "__main__":
    main()
