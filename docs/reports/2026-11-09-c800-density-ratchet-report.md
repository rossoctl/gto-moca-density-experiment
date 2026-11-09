# C800 density ratchet — Implementation report

Status: Complete <!-- for the 9-run C800 matrix; re-runs of 10pct-vm would be a new report -->
Date: 2026-11-09
Spec: none yet (manifest-driven ratchet; manifests `load800-mixed-{2,5,10}pct.json`)
Covers: the full C800 matrix — 3 churn rates × 3 variants (vm, fc-warm, fc-perexec) — 9 runs, 155,078 unit replays total

> **What this report says in one sentence.** At 2× C400's concurrency the per-backend
> behavior is unchanged (vm ~100% ok, fc 53/68/86% by churn rate), except at 10pct churn
> where the vm backend hits two identified walls — a driver-side `select()` fd limit
> (6,174 start failures) and genuine host memory pressure (4 qemus OOM-killed) — while
> fc-warm/perexec pass through the same point untouched.

**Append-only.** This document is not rewritten when a later phase changes the answer — write
a new report or append a dated section. See
[the lifecycle note](../README.md#specs-revise-reports-do-not).

---

## 1. What landed

| Run | Backend | Units | Ok | Wall |
| --- | ------- | ----- | -- | ---- |
| 2pct-vm | qemu | 18,792 | 18,792 (100%) | 2,337 s |
| 2pct-fc-warm | firecracker (warm) | 18,792 | 9,970 (53.1%) | 2,373 s |
| 2pct-fc-perexec | firecracker (per-exec) | 18,792 | 9,970 (53.1%) | 2,403 s |
| 5pct-vm | qemu | 19,786 | 19,778 (99.96%) | 2,329 s |
| 5pct-fc-warm | firecracker (warm) | 19,786 | 13,457 (68.0%) | 2,370 s |
| 5pct-fc-perexec | firecracker (per-exec) | 19,786 | 13,451 (68.0%) | 2,684 s |
| 10pct-vm | qemu | 15,876 | 8,879 (55.9%) | 2,401 s |
| 10pct-fc-warm | firecracker (warm) | 15,876 | 13,693 (86.3%) | 2,900 s |
| 10pct-fc-perexec | firecracker (per-exec) | 15,876 | 13,701 (86.3%) | 3,360 s |

All runs on srv-r18b14s27 (192 cores, 499 GiB RAM), driver `replay_manifest.py`,
launch procedure: vm as `galmasi` with `ulimit -n 65536`; fc under `sudo` (jailer needs
root) with `FC_VM_MODE=warm|perexec`; `sample_host.py` sampling 1 Hz alongside. Logs:
`replay/logs/load800/<variant>.{run.log,csv}` on the host (per-unit logs in the sibling
`manifest-load800-mixed-*` dirs).

## 2. Measurements

| What was measured | C400 counterpart | Measured at C800 | Method |
| ----------------- | ---------------- | ---------------- | ------ |
| vm ok-rate, 2pct | 100% | 100% | full manifest, one run each |
| vm ok-rate, 5pct | 100% | 99.96% (8 units, one flaky workload step) | same |
| vm ok-rate, 10pct | 94.7% | **55.9%** — see §4.1/§4.2 | same |
| fc ok-rate (both modes) | 53% / 68% / 87% at 2/5/10pct | 53.1% / 68.0% / 86.3% | same |
| fc warm vs perexec ok-rate | equal | equal (within 8 units at 5pct) | same |
| peak live sandboxes (2pct/5pct runs) | 441–449 (vm, 10pct) | 854–896 | `active N` maxima in run logs |
| peak host mem | 216 GiB (C400 vm) | 300 GiB (2pct-vm), 407 GiB (5pct-vm), ~499 GiB with ~100 GiB available (10pct-vm) | `sample_host.py` 1 Hz |
| peak CPU | 69% | 95–100% (5pct/10pct runs) | same |

The headline comparisons, stated falsifiably:

- fc failure fraction is **invariant across concurrency** (C100→C800) and across lifecycle
  mode, tracking only churn rate. At 2pct both fc modes produced the identical count
  9,970/18,792 — matching C400's 53%.
- fc memory advantage scales: peak 165 GiB (5pct-fc-warm) vs 407 GiB (5pct-vm) at equal
  concurrency — snapshot page-sharing holds at ~850–900 live microVMs.
- vm peak throughput per unit time was unchanged 2pct→5pct (~8.5 units/s).

## 3. Deltas from the design

1. **The C800 matrix was executed by a second operator session, not the original one.**
   Launch conventions were reconstructed from logs and `replay_manifest.py`; one deviation
   was found and corrected mid-campaign (missing `sample_host.py`, see §4.4). Accepted
   as-is.
2. **10pct-vm was not re-run after the `select()` wall was identified.** Fixing it needs a
   code change (`select()` → `selectors`/`poll` in the vm start path), which is a campaign
   decision, not an operator's. The wall is reported as a finding (§4.1) instead. Deferred.

## 4. Findings

### 4.1 Driver `select()` fd limit — the wall above NOFILE

- **Symptom:** 6,174/15,876 units (38.9%) in 10pct-vm fail instantly at sandbox start with
  `filedescriptor out of range in select()`; the ok-rate collapses to 55.9% with no
  resource exhaustion visible at the time of the first failures.
- **Root cause:** the vm sandbox-start path uses `select()`, whose fd_set is capped at
  FD_SETSIZE=1024. With the host NOFILE raised to 65,536 (the earlier C200-era fix), the
  driver's per-unit sockets now number past 1024 at 10pct churn. Same family as the
  `[Errno 24]` NOFILE wall found at ~330 live containers — raise one ceiling, meet the next.
- **Fix:** none applied. Port the start path to `selectors`/`poll` and re-run 10pct-vm.
  The fc backends' start paths do not hit this (10pct fc runs show no start-failed storm).

### 4.2 Real memory ceiling at 10pct-vm

- **Symptom:** kernel OOM-killed 4 qemus at ~19:16 during 10pct-vm; host available memory
  bottomed near 100 GiB.
- **Root cause:** ~1,100+ concurrent 1-GiB-ram qemus plus qcow2 page cache genuinely
  exhaust 499 GiB at 10pct churn. Unlike §4.1 this is a *density* result, not an
  artifact: the vm backend at C800 is near the host's memory ceiling at high churn.
- **Fix:** none possible without smaller guests or fewer concurrent units — this is the
  *measured wall*.

### 4.3 Flaky workload step (vm 5pct, 8 units)

- **Symptom:** exactly 8 failures in 5pct-vm, all `multi-source-data-merger` r1/r2/r3
  units, all at the same step (24.3 s), `patch_ok=True`.
- **Root cause:** the step recorded exit 0 but replays to 134 (SIGABRT) or 1
  nondeterministically — workload flakiness, not backend behavior. 8/19,786 = 0.04%.

### 4.4 Sampler `sandbox_count` column empty on 8 of 9 runs

- **Symptom:** `logs/load800/*.csv` have a blank `sandbox_count` column for every run
  launched by the second operator session; only 2pct-vm (first session's launch) has it.
- **Root cause:** the second session launched `sample_host.py` from a different context
  than the original launches; its sandbox counting (process-pattern based) found nothing.
  Peak-concurrency numbers for those runs come from `active N` maxima in the run logs
  instead, which are driver-reported and authoritative.
- **Fix:** noted; use run-log `active` maxima for C800 peaks.

### 4.5 fc failure class is environment divergence, not density

- **Symptom:** fc ok-rates of 53/68/86% by churn rate, stable across C100–C800 and both
  lifecycle modes.
- **Root cause:** per-unit logs show outcome mismatches like
  `/usr/local/bin/node: No such file or directory` in the snapshot rootfs vs the recorded
  container environment. The fraction tracks churn rate because longer-lived units
  accumulate more opportunities to hit a divergent step.
- **Fix:** a report, not a bug: fc density conclusions are sound; correctness parity with
  the container baseline is a separate workstream (rootfs parity).

## 5. What is NOT verified

- **10pct-vm's true ok-rate** — the 55.9% is dominated by the §4.1 driver artifact;
  a clean measurement needs the `select()` fix and a re-run (§3.2).
- **The vm backend above ~900 live** — no C800 run sustained more than ~900 concurrent
  qemus cleanly; the 10pct peak of 1,199 `active` is polluted by start-fail churn.
- **Podman at C800** — rootless podman was dropped from the matrix at C200 (keyring-quota
  wall, see `docs/notes/2026-11-08-rootless-podman-density-limits.md`); not retried.
- **Repeated runs / variance** — one run per cell; the 2pct fc warm/perexec identical
  counts (9,970 both) suggest low variance, but n=1 per cell is n=1.
- **fc `sandbox_count` series** — see §4.4; per-second live-VM curves for fc runs are not
  available, only run-log maxima.

## 6. Follow-ups

| What                | Why it was deferred | Issue |
| ------------------- | ------------------- | ----- |
| Port vm start path off `select()` and re-run 10pct-vm | mid-campaign code change, owner's call | — |
| fc rootfs parity with recorded container env (node et al.) | separate workstream from density | — |
| Update the (stale) overview doc with C20–C800 results | editorial; overview still says "no measurements yet" | — |
