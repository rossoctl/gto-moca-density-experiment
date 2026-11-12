"""1 Hz host CPU sampler for the microbenches: busy and iowait cores kept
SEPARATE (the campaign sampler folds iowait into busy -- the confound these
experiments exist to remove), plus per-pid CPU for tracked VM processes.

Usage (as a module):  s = Sampler("out.csv"); s.track(pid, "fc-12");
                      s.start(); ... s.stop()
CSV: epoch,tag,busy_cores,iowait_cores,pid_cpu_cores
`tag` is set via s.set_tag() between phases (variant/rung labels).
"""

import csv, os, threading, time

_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _stat_cpu():
    vals = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)   # idle + iowait
    iowait = vals[4] if len(vals) > 4 else 0
    return sum(vals), idle, iowait


def _pid_cpu(pid):
    """On-CPU seconds for the whole thread group, from the scheduler's own
    accounting (/proc/<pid>/task/*/schedstat field 0, ns).  NOT utime/stime
    and NOT /proc/stat: on this host's kernel the aggregate /proc/stat
    under-counts guest-mode execution ~10x (verified by pinning 16 idle
    qemus to one core: schedstat 27% vs /proc/stat 5.2%), so the campaign
    sampler's busy formula misses most vCPU time.  schedstat and per-pid
    utime agree with each other; schedstat also covers all threads."""
    try:
        import glob
        tot = 0
        for t in glob.glob(f"/proc/{pid}/task/*/schedstat"):
            tot += int(open(t).read().split()[0])
        return tot / 1e9 if tot else None
    except (OSError, ValueError, IndexError):
        return None


class Sampler:
    def __init__(self, path):
        self.path = path
        self.tag = ""
        self._pids = {}          # pid -> label (label kept in CSV? summed)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._f = open(path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(["epoch", "tag", "busy_cores", "iowait_cores",
                          "pid_cpu_cores", "tracked_alive"])

    def track(self, pid):
        with self._lock:
            self._pids[pid] = True

    def set_tag(self, tag):
        with self._lock:
            self.tag = tag

    def _once(self):
        total, idle, iowait = _stat_cpu()
        return total, idle, iowait

    def _loop(self):
        prev = self._once()
        prev_t = time.time()
        while not self._stop.wait(1.0):
            now = self._once()
            t = time.time()
            dt_tot = now[0] - prev[0]
            ncpu = os.cpu_count()
            busy = (dt_tot - (now[1] - prev[1])) / dt_tot * ncpu
            iow = (now[2] - prev[2]) / dt_tot * ncpu
            with self._lock:
                pids = list(self._pids)
                tag = self.tag
            pid_cpu = 0.0
            alive = 0
            for p in pids:
                c = _pid_cpu(p)
                if c is not None:
                    pid_cpu += c
                    alive += 1
            self._w.writerow([round(t, 1), tag, round(busy, 3),
                              round(iow, 3), round(pid_cpu, 3), alive])
            self._f.flush()
            prev, prev_t = now, t

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
        self._f.close()
