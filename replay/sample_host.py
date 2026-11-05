#!/usr/bin/env python3
"""sample_host.py -- host resource sampler for density runs.

Sampling (the default): one CSV row per tick, standalone by design -- start
it BEFORE a run and let it outlive the executor, so sampling can neither be
perturbed by the run nor perturb it.

  sample_host.py OUT.csv [--interval 1] [--disk-fs /]
                  [--count-cmd 'CMD'] [--qemu-rss]

  --disk-fs     the filesystem whose used bytes are sampled (default /):
                / on the container host, /tmp on the VM host (snapshot
                overlays live there)
  --count-cmd   a shell command printing the live sandbox count (e.g.
                "podman ps -q | wc -l", "pgrep -c qemu-system"); empty
                column when omitted
  --qemu-rss    add a column with the summed RSS of qemu processes in MB:
                committed (-m) vs touched memory is the density story for
                VM backends

Summarizing (--summarize): maxima over the whole CSV or an epoch window
(the run's plateau), the first usable density numbers.

  sample_host.py --summarize OUT.csv [--since EPOCH] [--until EPOCH]
"""
import argparse, csv, os, subprocess, sys, time

COLUMNS = ["epoch", "cpu_busy_pct", "mem_used_mb", "mem_avail_mb",
           "disk_used_mb", "sandbox_count", "load1", "qemu_rss_mb"]

def cpu_totals():
    vals = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    return sum(vals), idle

def meminfo():
    d = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        d[k] = int(v.split()[0])                          # kB
    return d

def disk_used_mb(fs):
    st = os.statvfs(fs)
    return round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e6, 1)

def run_count(cmd):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return int(out) if out.isdigit() else -1
    except Exception:
        return -1

def qemu_rss_mb():
    total = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if "qemu-system" not in f.read():
                    continue
            with open(f"/proc/{pid}/statm") as f:
                total += int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") / 1e6)
        except OSError:
            continue
    return round(total, 1)

def diskstats_totals():
    """(sectors read, sectors written) over real block devices (loop*, ram*
    excluded) -- deltas between ticks give host storage bandwidth."""
    rd = wr = 0
    for line in open("/proc/diskstats"):
        f = line.split()
        name = f[2]
        if name.startswith(("loop", "ram", "sr", "dm-")):
            continue
        rd += int(f[5])    # sectors read
        wr += int(f[9])    # sectors written
    return rd, wr


def podman_cgroup_mems():
    """(sum memory.current, sum current-inactive_file, sum io.stat read
    bytes, sum io.stat write bytes; memory in MB, io bytes raw counters)
    over the live rootless container cgroups (libpod-<id>.scope, conmon
    scopes excluded). 'current' counts page cache the containers faulted
    in; dropping inactive_file approximates the RAM truly in use -- pages
    the kernel could not reclaim without pressure (anon + active file +
    kernel). io.stat is cumulative; deltas between ticks give per-container
    storage bandwidth."""
    cur = true_ = 0.0
    io_rd = io_wr = 0
    base = "/sys/fs/cgroup/user.slice"
    for root, dirs, _files in os.walk(base):
        for d in list(dirs):
            if d.startswith("libpod-") and not d.startswith("libpod-conmon-"):
                g = os.path.join(root, d)
                try:
                    with open(os.path.join(g, "memory.current")) as f:
                        c = int(f.read())
                    inact = 0
                    with open(os.path.join(g, "memory.stat")) as f:
                        for line in f:
                            if line.startswith("inactive_file "):
                                inact = int(line.split()[1])
                                break
                    cur += c / 1e6
                    true_ += max(c - inact, 0) / 1e6
                    with open(os.path.join(g, "io.stat")) as f:
                        for line in f:
                            for tok in line.split()[1:]:
                                k, _, v = tok.partition("=")
                                if k == "read":
                                    io_rd += int(v)
                                elif k == "write":
                                    io_wr += int(v)
                except OSError:
                    continue
        dirs[:] = [d for d in dirs if d.startswith(("user.slice", "user-"))] \
            if root == base else dirs
    return round(cur, 1), round(true_, 1), io_rd, io_wr


def sample(out_path, interval, disk_fs, count_cmd, with_qemu_rss,
           with_podman_mem, with_disk_io):
    total_prev, idle_prev = cpu_totals()
    rd_prev, wr_prev = diskstats_totals()
    io_prev = (0, 0)
    first_tick = True
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        cols = COLUMNS[:]
        if with_podman_mem:
            cols += ["podman_cg_mb", "podman_true_mb",
                     "podman_rd_mbps", "podman_wr_mbps"]
        if with_disk_io:
            cols += ["disk_rd_mbps", "disk_wr_mbps"]
        w.writerow(cols)
        while True:
            time.sleep(interval)
            try:
                total, idle = cpu_totals()
                busy = 100.0 * (1 - (idle - idle_prev) / max(total - total_prev, 1))
                total_prev, idle_prev = total, idle
                m = meminfo()
                load1 = open("/proc/loadavg").read().split()[0]
                row = [int(time.time()), round(busy, 1),
                       round((m["MemTotal"] - m["MemAvailable"]) / 1024),
                       round(m["MemAvailable"] / 1024),
                       disk_used_mb(disk_fs),
                       run_count(count_cmd) if count_cmd else "",
                       load1,
                       qemu_rss_mb() if with_qemu_rss else ""]
                if with_podman_mem:
                    cg, true_, io_rd, io_wr = podman_cgroup_mems()
                    if first_tick:
                        io_prev = (io_rd, io_wr)
                        pr = pw = 0.0
                    else:
                        pr = max(io_rd - io_prev[0], 0) / 1e6 / interval
                        pw = max(io_wr - io_prev[1], 0) / 1e6 / interval
                        io_prev = (io_rd, io_wr)
                    row += [cg, true_, round(pr, 1), round(pw, 1)]
                if with_disk_io:
                    rd, wr = diskstats_totals()
                    dr = (rd - rd_prev) * 512 / 1e6 / interval
                    dw = (wr - wr_prev) * 512 / 1e6 / interval
                    rd_prev, wr_prev = rd, wr
                    row += [round(max(dr, 0), 1), round(max(dw, 0), 1)]
                first_tick = False
                w.writerow(row)
                fh.flush()
            except KeyboardInterrupt:
                return
            except Exception as e:
                w.writerow([int(time.time()), f"error: {e}"])
                fh.flush()

def summarize(path, since, until):
    rows = [r for r in csv.DictReader(open(path))]
    if since or until:
        rows = [r for r in rows
                if (not since or int(r["epoch"]) >= since)
                and (not until or int(r["epoch"]) <= until)]
    if not rows:
        sys.exit("no rows in window")

    def fmax(col):
        vals = [float(r[col]) for r in rows if r.get(col) not in ("", None)]
        return max(vals) if vals else None

    counts = [int(r["sandbox_count"]) for r in rows
              if r.get("sandbox_count") not in ("", None, "-1")]
    print(f"window: {len(rows)} ticks "
          f"({rows[0]['epoch']}..{rows[-1]['epoch']})")
    print(f"peak cpu_busy_pct:    {fmax('cpu_busy_pct')}")
    print(f"peak mem_used_mb:     {fmax('mem_used_mb')}")
    print(f"peak disk_used_mb:    {fmax('disk_used_mb')}")
    print(f"peak load1:           {fmax('load1')}")
    if fmax("qemu_rss_mb") is not None:
        print(f"peak qemu_rss_mb:     {fmax('qemu_rss_mb')} (touched; "
              f"committed is -m x count)")
    if fmax("podman_cg_mb") is not None:
        print(f"peak podman_cg_mb:    {fmax('podman_cg_mb')} "
              f"(cgroup current, page cache included)")
        print(f"peak podman_true_mb:  {fmax('podman_true_mb')} "
              f"(current - inactive_file: RAM truly in use)")
        for col in ("podman_rd_mbps", "podman_wr_mbps",
                    "disk_rd_mbps", "disk_wr_mbps"):
            if fmax(col) is None:
                continue
            vals = [float(r[col]) for r in rows
                    if r.get(col) not in ("", None)]
            print(f"peak {col}:  {fmax(col):8.1f}   "
                  f"mean {sum(vals)/len(vals):8.1f}")
    if counts:
        print(f"peak sandbox_count:   {max(counts)}")
        pm, pc = fmax("mem_used_mb"), max(counts)
        if pm:
            print(f"=> peak MB per sandbox: {pm / max(pc, 1):.0f}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="CSV path (sample) or with --summarize")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--disk-fs", default="/")
    ap.add_argument("--count-cmd", default=None)
    ap.add_argument("--qemu-rss", action="store_true")
    ap.add_argument("--podman-mem", action="store_true",
                    help="sum container cgroup memory (current, and "
                         "current-inactive_file as true-use estimate); "
                         "also container io.stat read/write bandwidth")
    ap.add_argument("--disk-io", action="store_true",
                    help="host storage bandwidth from /proc/diskstats "
                         "deltas (real devices, loop/ram/dm excluded)")
    ap.add_argument("--since", type=int)
    ap.add_argument("--until", type=int)
    args = ap.parse_args()
    if args.summarize:
        summarize(args.path, args.since, args.until)
    else:
        print(f"sampling to {args.path} every {args.interval}s "
              f"(Ctrl-C to stop)", file=sys.stderr)
        sample(args.path, args.interval, args.disk_fs, args.count_cmd,
               args.qemu_rss, args.podman_mem, args.disk_io)

if __name__ == "__main__":
    main()
