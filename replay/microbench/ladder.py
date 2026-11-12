"""Experiment 1 + 2: the idle-VM ladder.

Holds K idle VMs (K = 0, 16, 32, 64) and samples host CPU per rung, for four
variants:

  fc-run     firecracker, restored from the golden snapshot and resumed
             (vsock agent live, no workspace mount -- the resident-warm
             steady state minus the workload)
  fc-paused  firecracker, snapshot loaded with resume_vm=false (no vCPU
             thread runs -- if this costs ~0, the running-idle tax is
             guest-side, i.e. timer exits, not VMM machinery)
  qemu-run   qemu VMs resumed from the prep state, sitting at the shell
  qemu-paused the same, stopped via QMP after boot

Slope of busy cores vs K = the fixed per-VM tax, driver-free.  fc VM pids are
tracked per-process so VMM cost and global busy are both on record.

Usage (ROOT required for the jailer; from the repo root):
  sudo env FC_MAX_VMS=64 python3 -I replay/microbench/ladder.py \
      --variant fc-run --out /root/mb/out
The qemu variants run as the user (state.bin resumes need the saving user's
environment) with the default VM_SMP=2 -- the campaigns' configuration
(this image's state.bin is SMP=2-pinned; VM_SMP=1 hangs the resume).
"""

import argparse, os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # replay/
sys.path.insert(0, HERE)                           # microbench/

SNAPSHOT = "tb-constraints-scheduling"             # smallest golden snapshot
VM_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                      "images", "tb30", "constraints-scheduling", "vm")
SETTLE_S = 45
MEASURE_S = 150
RUNGS = [0, 16, 32, 64]


def run_fc_rung(K, paused, sampler, fcmod):
    snapdir = os.path.join(fcmod.FC_SNAPSHOTS, SNAPSHOT)
    # one shared, never-mounted workspace image: the jail only hardlinks it
    ws_dir = os.path.join(fcmod.FC_BASE, "microbench-ws")
    os.makedirs(ws_dir, exist_ok=True)
    ws = os.path.join(ws_dir, "workspace.img")
    if not os.path.exists(ws):
        with open(ws, "wb") as f:
            f.truncate(2 * 1024 * 1024 * 1024)
        subprocess.run(["mkfs.ext4", "-q", "-F", ws], check=True)
    vms = []
    for i in range(K):
        vm = fcmod.FcVM(snapdir, ws)
        vm.start(resume=not paused, mount_workspace=False)
        vms.append(vm)
        sampler.track(vm.proc.pid)
        if i % 8 == 7:
            time.sleep(0.5)
    return vms


def run_qemu_rung(K, paused, sampler, replay):
    sbs = []

    def boot(i):
        sb = replay.VmSandbox(f"mbrung{i}", VM_DIR)
        sb.start()
        if paused:
            qmp_stop(sb.sock + ".mon")
        with sampler_lock:
            sampler.track(int(open(sb.sock + ".pid").read()))
            sbs.append(sb)
    sampler_lock = threading.Lock()
    # boot in waves of 8: 64 sequential boots would take ~15 min
    for wave in range(0, max(K, 1), 8):
        ths = [threading.Thread(target=boot, args=(i,))
               for i in range(wave, min(wave + 8, K))]
        for t in ths: t.start()
        for t in ths: t.join()
    return sbs


def qmp_stop(mon_sock):
    """HMP `stop`: freeze the vCPU threads post-boot (the qemu-paused arm).
    The -monitor socket is the HUMAN monitor, not QMP -- JSON commands come
    back 'unknown command' and the VM keeps running (the first qemu-paused
    ladder silently measured running VMs)."""
    import socket as s
    c = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    c.settimeout(30)
    c.connect(mon_sock)
    c.sendall(b"stop\n")
    time.sleep(0.5)          # HMP replies asynchronously; best effort
    c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True,
                    choices=["fc-run", "fc-paused", "qemu-run", "qemu-paused"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--rungs", default=None,
                    help="comma-separated K list (default 0,16,32,64); "
                         "e.g. --rungs 32 for a single held rung")
    args = ap.parse_args()
    rungs = ([int(x) for x in args.rungs.split(",")]
             if args.rungs else RUNGS)
    os.makedirs(args.out, exist_ok=True)

    import cpu_sampler
    sampler = cpu_sampler.Sampler(
        os.path.join(args.out, f"ladder-{args.variant}.csv"))
    sampler.start()
    backend = args.variant.split("-")[0]
    paused = args.variant.endswith("paused")
    mod = None
    if backend == "fc":
        import fc as fcmod
        mod = fcmod
    else:
        import replay as rmod
        mod = rmod

    try:
        for K in rungs:
            sampler.set_tag(f"{args.variant}-K{K:03d}-settle")
            print(f"[{time.strftime('%H:%M:%S')}] {args.variant} K={K}: "
                  f"launching...", flush=True)
            if backend == "fc":
                held = run_fc_rung(K, paused, sampler, mod)
            else:
                held = run_qemu_rung(K, paused, sampler, mod)
            time.sleep(SETTLE_S)
            sampler.set_tag(f"{args.variant}-K{K:03d}-measure")
            print(f"[{time.strftime('%H:%M:%S')}] {args.variant} K={K}: "
                  f"measuring {MEASURE_S}s", flush=True)
            time.sleep(MEASURE_S)
            print(f"[{time.strftime('%H:%M:%S')}] {args.variant} K={K}: "
                  f"teardown", flush=True)
            sampler.set_tag(f"{args.variant}-K{K:03d}-teardown")
            threads = []
            for h in held:
                stop = h.stop if backend == "fc" else h.stop
                th = threading.Thread(target=stop, daemon=True)
                th.start(); threads.append(th)
            for th in threads:
                th.join(60)
            time.sleep(15)                          # let teardown IO drain
    finally:
        sampler.stop()
        print("done", flush=True)


if __name__ == "__main__":
    main()
