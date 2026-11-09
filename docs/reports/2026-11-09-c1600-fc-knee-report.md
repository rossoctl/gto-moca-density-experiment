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

