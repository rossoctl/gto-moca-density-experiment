# C1600 fc knee — Implementation report

Status: Complete
Date: 2026-11-09
Spec: none (manifest ratchet; manifest `load1600-mixed-2pct.json`, seed 21 —
identical population/duty/duration to `load800-mixed-2pct.json`, concurrency doubled)
Covers: 2 runs — fc-warm and fc-perexec at C1600, 2% churn — 75,161 unit replays

> **What this report says in one sentence.** Firecracker clears C1600 at 2% churn in the
> warm lifecycle (1,762 live microVMs, CPU peak 84%, memory peak only 131 GiB — 0.074 GiB
> per live VM) with the usual 53% ok-rate, but the per-exec lifecycle collapses to 33.6%
> on workspace-mount and jailer failures under IO saturation — the two lifecycles diverge
> at exactly the doubling where the warm one stops scaling on CPU.

**Append-only.** This document is not rewritten when a later phase changes the answer — write
a new report or append a dated section. See
[the lifecycle note](../README.md#specs-revise-reports-do-not).

---

## 1. What landed

| Run | Units | Ok | Wall | Peak live | Peak mem | Peak CPU | Peak load1 |
| --- | ----- | -- | ---- | --------- | -------- | -------- | ---------- |
| 2pct-fc-warm | 37,583 | 19,935 (53.0%) | 2,378 s | 1,762 | 131 GiB | 84% | 153 |
| 2pct-fc-perexec | 37,578 | 12,636 (33.6%) | 3,709 s | (unreliable¹) | 42 GiB | 84% | 540 |

Host and method identical to
[the C800 report](2026-11-09-c800-density-ratchet-report.md); fc runs under sudo
with `FC_VM_MODE`, `sample_host.py` 1 Hz, launch output consolidated to
`replay/logs/load1600/` on the host.

¹ perexec's driver-reported `active` (peak 17,072) counts very short-lived VMs and is
not comparable; warm's 1,762 is the trustworthy concurrency figure.

## 2. Measurements

| What was measured | Prediction (from C800 extrapolation) | Measured | Method |
| ----------------- | ------------------------------------ | -------- | ------ |
| fc-warm ok-rate at 2pct | 53% | 53.0% (19,935/37,583) | full manifest |
| fc-warm peak live | ~1,600 | 1,762 | `active N` maxima, run log |
| fc-warm peak CPU | ~100% (marginal) | 84% | sampler 1 Hz |
| fc-warm mem per live VM | ~0.18 GiB (falling) | **0.074 GiB** (131 GiB / 1,762) | sampler |
| fc-perexec ok-rate at 2pct | 53% | **33.6%** — new degradation | full manifest |
| fc-perexec wall time | ~2,400 s | 3,709 s (+56% vs warm) | driver |

The memory-sharing trend is the headline: per-live-VM host memory fell 0.26 → 0.18 → 0.074 GiB
across C100 → C800 → C1600. Snapshot page sharing amortizes with concurrency; memory will
not bind for firecracker on this host at any plausible ratchet. CPU is the warm lifecycle's
binding constraint: 84% at C1600 means C3200 would saturate.

## 3. Deltas from the design

1. **Only 2 of the usual 9 matrix cells were run** (2pct fc-warm, 2pct fc-perexec) — the run
   was commissioned as a targeted knee probe, not a full ratchet. Accepted.

## 4. Findings

### 4.1 fc-perexec collapses at C1600: workspace mounts and jailer aborts

- **Symptom:** ok-rate falls from 53.1% (C800) to 33.6%; wall time +56% vs warm; host load1
  peaks at 540 against 192 cores while CPU busy peaks at only 84% — processes blocked on IO,
  not runnable.
- **Root cause (failure census over 24,942 failed units):** 12,048 units fail on the known
  environment-divergence mismatches (unchanged fraction); the *new* failures are
  infrastructure: ~4,230 `mount /workspace failed rc=32: can't read superblock on /dev/vdb`
  and ~1,340 `jailer/firecracker exited rc=-6 waiting for firecracker API socket`. Per-exec
  pays the full restore+jail+mount machinery per tool step; at C1600's arrival rate that
  machinery saturates the disk path — transient disk use peaked at 959 GB (vs 221 GB warm).
- **Fix:** none applied. The warm lifecycle is the scaling path; per-exec on this host has a
  practical ceiling between C800 and C1600 at 2% churn unless the per-step restore cost is
  reduced (moca's warm-snapshot model exists precisely for this).

### 4.2 fc-warm's CPU knee is between C1600 and C3200

- **Symptom:** CPU peak 84% at 1,762 live (avg 49%); load1 peak 153/192.
- **Root cause:** none — this is the measured scaling curve, not a failure.
- **Fix:** n/a. Prediction for a C3200 2pct probe: CPU saturation (~100%), memory still
  comfortable (~250 GiB at current amortization).

### 4.3 Sampler gaps recur (from C800 §4.4)

- **Symptom:** `sandbox_count` and `disk_{rd,wr}_mbps` blank in both C1600 CSVs.
- **Root cause:** same launch-context issue as C800 §4.4. Peak-live and disk-used figures
  come from run logs and the `disk_used_mb` column respectively.
- **Fix:** still open; anyone re-launching samplers should verify the columns populate.

## 5. What is NOT verified

- **fc-warm at C3200** — predicted CPU-bound but not run.
- **fc-perexec between C800 and C1600** — the exact ceiling is bracketed, not located.
- **fc-warm at 5/10pct churn at C1600** — not run; higher churn raises CPU per unit and
  would bind sooner than the 2pct figures suggest.
- **Root cause of the `/dev/vdb` mount failures** — correlated with IO saturation and
  959 GB transient disk use, but the fs was not inspected during the failure window
  (no `df`/dmesg capture at peak); "saturation" is inference from load1 and disk_used.
- **vm backend at C1600** — not attempted; the C800 10pct memory ceiling (~1,100 × 1 GiB
  guests) already bounds it below C1600 at high churn.

## 6. Follow-ups

| What                | Why it was deferred | Issue |
| ------------------- | ------------------- | ----- |
| C3200 2pct fc-warm probe | CPU-knee confirmation | — |
| Instrument the fc workspace-mount path under load | perexec ceiling is bracketed, not explained to root cause | — |
| Fix sampler columns (sandbox_count, disk rates) | recurring data gap since C800 | — |

---

## Appendix — 2026-11-11: the perexec "IO saturation" was disk-full (ENOSPC), and the C1600 ceiling moves with capacity

### A. Forensics on the original run: capacity exhaustion, not IO saturation

§4.1 attributed the perexec collapse to the disk path saturating; §5 left the root cause of
the `/dev/vdb` mount failures open. Both are resolved by re-examining the original run's
collected artifacts (no rerun needed):

- The root filesystem is 982 GB (decimal); the run's sampler shows disk use frozen at
  **959 GB used = 97.7% full for 9 minutes** (t≈1205–1750 in the CSV) — a full disk, not
  throttled writeback. Sustained fill rate was only ~12 MB/s: never a bandwidth or IOPS
  problem.
- **4,282 per-unit logs contain `No space left on device`** — from the driver
  (`OSError(28)`, including 520 units that died *writing their own logs* to the same full
  fs), the jailer (`Write("/firecracker.pid", StorageFull)`, `MkdirOldRoot`), and
  firecracker. The rc=32 "can't read superblock" / "Structure needs cleaning" (EUCLEAN)
  failures are what ext4 mount and journal operations look like on a full filesystem.
- rc=32 failures cluster in exactly two windows: a burst at t≈1080–1200 (when the disk
  tops out) and a sustained block at t≈1800–3120 (the deletion-drain, units fighting for
  freed space). During the full-disk plateau itself, almost none — nothing can even try.
- The load1-540-at-84%-CPU signature is D-state processes on ENOSPC/retry paths and the
  deletion storm, not device queue saturation.

Why perexec filled the disk where warm did not: every fc sandbox seeds a private 2-GiB
`workspace.img` with a full testbed copy; perexec's slowdown stretches unit residency and
multiplies concurrent workspaces (and `stop()`'s
`shutil.rmtree(..., ignore_errors=True)` would silently leak any workspace whose cleanup
failed mid-storm). Warm peaked at 221 GB on the same units.

### B. The fix: FC_BASE on the spare NVMe

The host's second NVMe (nvme1n1, 3.6T ext4, previously idle and unmounted, 2.9T free) now
hosts a second fc base: golden snapshots copied to `/mnt/moca/snapshots` (22G,
size-verified; same-host copies are fine — the "host-pinned" caveat is about crossing
hosts), runs launched with `FC_BASE=/mnt/moca/purefc FC_SNAPSHOTS=/mnt/moca/snapshots`.
Both are plain env vars in `replay/fc.py`; no code change. Smoke manifest on the new base:
5/6 ok with the one documented regex-log divergence — byte-identical behavior to the
original base.

### C. The re-run: the ceiling was capacity, and it is gone

Same manifest bytes (`load1600-mixed-2pct.json`), same launch conventions, perexec
lifecycle, sampler on `/mnt`. Original run's artifacts renamed aside (`*.run1.*`). Host
otherwise idle.

| 2pct-fc-perexec @ C1600 | original (11-09, OS disk) | re-run (11-11, /mnt/moca) | warm baseline |
| ---------------------- | ------------------------- | ------------------------ | ------------- |
| units ok | 12,636/37,578 (33.6%) | **19,931/37,583 (53.0%)** | 19,935 (53.0%) |
| wall time | 3,709 s | 4,597 s | 2,378 s |
| rc=32 / ENOSPC failures | ~4,230 + ~1,340 jailer aborts | **0** (not one, in 37,583 units) | 0 |
| peak disk used | 959 GB (97.7% of fs — the cap) | 2,084 GB of 3.6T (~1.5 TB true transient) | 221 GB |
| peak CPU | 84% | 84.5% | 84% |
| peak load1 | 540 | 545 | 153 |
| peak mem | 42 GiB | 41 GiB | 131 GiB |

Readings:

1. **The ok-rate recovers to the warm baseline exactly** (19,931 vs 19,935 — four units).
   Perexec's C1600 "ceiling" was never a lifecycle limit; it was a 982 GB disk. With
   capacity, perexec and warm fail on the same environment-divergence class and nothing
   else — §4.5's invariance claim now extends to C1600 perexec.
2. **The workload's true transient is ~1.5 TB** — the original 959 GB was the disk cap,
   not the demand. The C800→C1600 perexec bracket of §5 is explained: C800's ~1,100 live
   × ~1 GB workspaces fit the OS disk; C1600's did not.
3. **Wall time got longer, not shorter** (4,597 s vs 3,709 s): the original "finished"
   faster because 24,942 units died early and cheaply. The honest comparison is against
   warm's 2,378 s: perexec still pays ~1.9× wall at equal ok-rate, and load1 still peaks
   ~545 against 84.5% CPU — a real D-state population in the per-step jail/mount/teardown
   machinery that no longer *fails* units but still costs time. The perexec wall-time gap
   is the remaining open question (IO latency of the jail churn is the candidate; the
   IOPS question this appendix started from is still unmeasured, just no longer
   failure-causing).
4. Peak `sandbox_count` (pgrep) 295 and driver `active` peak 21,056 are both unreliable
   for perexec, as before (§1's footnote): pgrep samples a fast-churning process table,
   `active` counts very short-lived VMs.

Not verified: whether the 1.5 TB transient is purely live workspaces or includes
in-flight-VM-held (deleted-but-open) images — needs a workspace-bytes column in a future
run; whether perexec wall time converges toward warm on faster storage (the load1
signature suggests an IO-latency component worth one instrumented run with iostat/PSI);
5/10pct churn at C1600 perexec on the new base.

