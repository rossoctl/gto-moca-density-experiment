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

def sample(out_path, interval, disk_fs, count_cmd, with_qemu_rss):
    total_prev, idle_prev = cpu_totals()
    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        while True:
            time.sleep(interval)
            try:
                total, idle = cpu_totals()
                busy = 100.0 * (1 - (idle - idle_prev) / max(total - total_prev, 1))
                total_prev, idle_prev = total, idle
                m = meminfo()
                load1 = open("/proc/loadavg").read().split()[0]
                w.writerow([int(time.time()), round(busy, 1),
                            round((m["MemTotal"] - m["MemAvailable"]) / 1024),
                            round(m["MemAvailable"] / 1024),
                            disk_used_mb(disk_fs),
                            run_count(count_cmd) if count_cmd else "",
                            load1,
                            qemu_rss_mb() if with_qemu_rss else ""])
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
    ap.add_argument("--since", type=int)
    ap.add_argument("--until", type=int)
    args = ap.parse_args()
    if args.summarize:
        summarize(args.path, args.since, args.until)
    else:
        print(f"sampling to {args.path} every {args.interval}s "
              f"(Ctrl-C to stop)", file=sys.stderr)
        sample(args.path, args.interval, args.disk_fs, args.count_cmd,
               args.qemu_rss)

if __name__ == "__main__":
    main()
