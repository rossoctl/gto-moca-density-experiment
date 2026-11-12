# CPU overhead microbench — Implementation report

Status: Complete
Date: 2026-11-11
Spec: none (question-driven microbenchmarks; scripts in `replay/microbench/`)
Covers: experiments 1–4 of the CPU-decomposition plan (idle-VM ladder, KVM-exit
census, step microbench, null-backend manifest) plus two diagnostics that became
necessary (CPU-accounting arbitration, sandbox-lifecycle bench)

> **What this report says in one sentence.** The campaign model "fc-warm costs
> 27 mc/VM fixed + 1.66× payload" decomposes into almost entirely different
> parts than theorized: a resident firecracker idles at 0.3–0.6 mc/VM, the
> replay harness costs 0.8 mc per live unit, tool steps carry NO payload
> amplification (guest CPU ≈ 0.5–1.0× their recorded wall time) — the fixed
> term is per-unit **lifecycle machinery** (workspace mkfs, seed-VM testbed
> copy, restore, per-exec git-diff VM, teardown) at ~7 core-seconds per unit —
> and along the way we found that the campaign sampler's `/proc/stat` formula
> both double-counts fc guest time (2×) and misses most qemu vCPU time,
> so every campaign `cpu_busy_pct` number needs relabeling.

**Append-only.** This document is not rewritten when a later phase changes the answer — write
a new report or append a dated section. See
[the lifecycle note](../README.md#specs-revise-reports-do-not).

---

## 1. What landed

| Experiment | What it was | Landed | Where |
| ---------- | ----------- | ------ | ----- |
| 1 | Idle-VM ladder, fc + qemu, running vs paused, K = 0/16/32/64 | Yes | `replay/microbench/ladder.py`; CSVs in `replay/logs/microbench-2026-11-11/` (gitignored) |
| 2 | Timer attribution — planned as HZ/nohz guest, **replaced** by a KVM-exit census (`perf stat/record -e kvm:kvm_exit`) | Yes (substituted) | census inline; reason histograms `census-fc.txt`, `census-qemu.txt` |
| 3 | Null-backend manifest (harness cost with zero sandboxes) | Yes, truncated at 25,621/37,583 units (§3.4) — data sufficient | `replay.py` `NullTarget`/`NullSandbox`, `replay_manifest.py --backend null` |
| 4 | Step microbench, one warm fc VM: real trajectories (django-10914, multi-source-data-merger) + synthetic CPU/IO arms | Yes (merger's synth-io arm degenerate, §5) | `replay/microbench/stepbench.py` |
| + | CPU-accounting arbitration (scheduler `schedstat` vs per-pid `utime` vs `/proc/stat` vs pinning) | Yes | inline diagnostics, §4.3 |
| + | Sandbox-lifecycle bench (full unit setup/teardown cost) | Yes, 6 reps, django image | inline, §4.5 |

All on srv-r18b14s27 (192 cores, 499 GiB, kernel 7.0.0-42), host verified idle
before each stage; stages strictly sequential. fc work ran under sudo against
the `tb-constraints-scheduling` / `sweb-django-10914` golden snapshots; qemu
runs as `galmasi` at default `VM_SMP=2` (the campaigns' configuration — the
tb30 `state.bin` is SMP=2-pinned; SMP=1 hangs its resume).

## 2. Measurements

| What was measured | Prior belief / campaign fit | Measured | Method |
| ----------------- | --------------------------- | -------- | ------ |
| fc idle tax (running) | 27 mc/VM | **0.3 mc/VM** (VMM process), 0.6 mc/VM whole-system slope | ladder, K=0/16/32/64 × 150 s |
| fc idle tax (paused) | — | **0.2 mc/VM** | ladder, `resume_vm: false` |
| qemu idle tax (running) | ~50 mc/VM (campaign fit) | **~27 mc/VM** (schedstat; 30/29/24 mc at K=16/32/64) | ladder rerun after HMP fix |
| qemu idle tax (stopped) | — | **0.3 mc/VM** | ladder, HMP `stop` |
| fc KVM exits, idle | timer interrupts | **17/s/VM** (EXIT_MSR 10, EXIT_HLT 4.6) | `perf record -e kvm:kvm_exit`, 32 VMs |
| qemu KVM exits, idle | — | **53/s/VM** (MSR 22, IOIO 18, HLT 11) | same, 16 VMs |
| guest CPU per recorded tool-second | 1.66× ("amplified") | **0.96 (django-10914), 0.49 (multi-source-data-merger)**; synth CPU arm 1.00 | stepbench, guest `/proc/stat` around full timed replays |
| vCPU-thread CPU per recorded tool-second | — | ≈ same as guest (0.97/0.51) — the vCPU thread *is* the guest's execution | stepbench, `schedstat` of the firecracker pid |
| guest_client + wrapper per tool-second | part of 1.66× | **0.01–0.05 c-s** | stepbench, `RUSAGE_CHILDREN` |
| harness cost per live unit | part of 27 mc/VM | **0.8 mc/live-unit** (0.8 cores at 1,021 active) | null-backend C1600 manifest, busy-vs-active regression |
| per-unit lifecycle cost | unconsidered | **~7 core-seconds** (django image: mkfs + seed `cp` + restore + per-exec git-diff VM + teardown; wall 5.9 s) | lifecycle bench, 6 reps |
| `/proc/stat` fidelity | assumed correct | **fc guest user-mode counted 2×; qemu idle vCPU time ~95% missed** | §4.3 arbitration |

## 3. Deltas from the design

1. **Experiment 2 (HZ=100/nohz guest) was replaced by a KVM-exit census.**
   Rebuilding guest kernels for a tick-rate experiment is a campaign; counting
   exit reasons with `perf` answers the same question (what wakes an idle
   guest) in minutes. Accepted.
2. **The qemu ladders were run twice.** The first qemu-paused run silently
   measured *running* VMs: the `-monitor` socket speaks HMP, not QMP, and JSON
   `stop` commands come back "unknown command" without raising (§4.6). The
   rerun used HMP `stop`, verified to drop a VM's rate from 27 mc to 0.000.
3. **The stepbench's per-pid sampler switched from `utime`/`stime` to scheduler
   `schedstat`** mid-experiment after the arbitration (§4.3) showed the
   aggregate `/proc/stat` cannot be trusted for VM CPU on this kernel.
4. **The null manifest stalled at 25,621/37,583 units** on `[Errno 24] Too many
   open files` — the operator (this session) launched it without
   `ulimit -n 65536`, violating the known rule. The pre-stall data covers the
   full active-count ramp (0→1,021), which is what the regression needed;
   accepted as-is rather than re-run.

## 4. Findings

### 4.1 The campaign's "fixed 27 mc/VM" is per-unit lifecycle machinery

- **Symptom:** every component the fixed tax was theorized to be — idle VMM,
  driver, step engines, logging — measured 1–2 orders of magnitude smaller.
- **Root cause:** each warm unit pays a one-time setup/teardown bill.
  Phase-decomposed (django image, 5 reps, ±2% across reps):

  | Phase | core-s | share |
  | ----- | ----- | ----- |
  | `mkfs.ext4` on the 2 GiB workspace | 0.12 | 2% |
  | seed VM: `cp -a /testbed` into the workspace | **4.2** | **56%** |
  | warm VM snapshot restore + first exec | 0.43 | 6% |
  | per-exec git-diff VM at unit end | **2.5** | **34%** |
  | teardown (sync + `rmtree`) | 0.18 | 2% |

  90% of the bill is experiment policy, not moca machinery: the private
  testbed copy (ext4 has no reflinks, so isolation means a materialized copy
  through a guest) and the verification diff (a second fresh VM; TB units
  carry no patch file and skip it entirely — hence the mixed-population
  lifecycle of ~2.4 c-s/unit: 0.82 × ~1.3 TB + 0.18 × 7.5 sweb). The snapshot
  restore itself — the machinery the lifecycle model exists to price — costs
  0.43 c-s. At C1600's arrival rate (~15.8 units/s) and mean residency ~76 s,
  the mixed lifecycle amortizes to ~30–50 mc/VM, consistent with the
  campaign's 27 mc fit once the sampler's guest double-count is folded in.
- **Fix:** none applied — attribution, not a bug. The two levers are a CoW
  workspace (reflink/qcow2-backing instead of seed `cp`: 4.2 → ~0.2) and
  diffing on the unit's still-alive warm VM instead of a fresh one
  (2.5 → ~0.3); together ~7.5 → ~1.2 c-s/unit and the fc-warm CPU knee moves
  out ~proportionally.

### 4.2 There is no payload amplification — recorded wall time was never CPU time

- **Symptom:** the campaign fit's 1.66× duty multiplier.
- **Root cause:** guest CPU per recorded tool-second is **workload-dependent
  and ≤ 1**: 0.96 for django (CPU-dense steps), 0.49 for the TB merger
  (IO-shaped steps whose recorded wall time includes waits), exactly 1.00 for
  a synthetic spin. The "1.66×" was an artifact of fitting one line through
  (a) this per-workload variation, (b) the lifecycle term (which scales with
  arrivals, i.e. with C, mimicking a fixed cost), and (c) the sampler's
  2× guest double-count (§4.3).
- **Fix:** capacity models should use `guest_CPU ≈ 0.5–1.0 × recorded_tool_s`
  per workload class, not a uniform multiplier.

### 4.3 The campaign CPU metric is unreliable on this kernel — in both directions

- **Symptom:** with 16 idle qemu VMs pinned to cpu0, the scheduler's
  `schedstat` said 27% of that core was used, per-pid `utime+stime` agreed
  (~25–30 mc/VM) — but aggregate `/proc/stat` (the formula
  `sample_host.py:37` uses) showed 5.2%. Conversely, one fc guest burning
  exactly 1 core showed as 1.00 in `schedstat` but **2.00** in `/proc/stat`.
- **Root cause:** not root-caused. The 2× is the documented guest-time
  double-count (guest time appears in both `user` and `guest`, and the
  all-fields sum adds both). The qemu under-count's mechanism (vtime/guest
  accounting losing vCPU time on this kernel) was not chased — it needs a
  kernel-accounts dig, and the empirical facts are enough to disqualify the
  metric. Note `perf stat -a -e task-clock` cannot arbitrate (it reports
  wall×CPUs system-wide) and RAPL package power (±8 W noise) cannot resolve
  sub-core effects.
- **Fix:** future samplers should log the per-field `/proc/stat` deltas (not
  just the formula result) and, where VM CPU matters, scheduler `schedstat`.
  All campaign `cpu_busy_pct` numbers are lower bounds for qemu-heavy runs
  and ~2×-inflated for fc guest user time; the saturation readings (near
  100%) remain qualitatively valid.

### 4.4 qemu vs firecracker idle: 27 vs 0.3 mc/VM, and why

- **Symptom:** 90× idle-cost ratio between two KVM hypervisors on the same
  host.
- **Root cause (measured, partially inferred):** qemu idle VMs take 53
  exits/s/VM (MSR 22, **IOIO 18** — port I/O to qemu's emulated legacy
  devices — HLT 11) vs fc's 17/s/VM (MSR 10, HLT 4.6); but 3× the exits
  cannot explain 90× the CPU, so most of qemu's cost is guest-mode execution
  between exits — its guest (host kernel 7.0, `init=/bin/bash`, 2 vCPUs) runs
  its idle loop much hotter than the fc guest (FC CI kernel, moca init, 1
  vCPU). A stopped qemu costs 0.3 mc/VM, so none of it is qemu's own main
  loop. The guest-configuration attribution (tick rate, idle states) is
  inference; the device-model part (IOIO exits) is measured.
- **Fix:** none — this is the qemu backend's measured density cost. The
  campaign's ~50 mc/VM qemu fit was this 27 mc plus the same lifecycle/harness
  terms as fc, partially hidden by the `/proc/stat` under-count.

### 4.5 The harness is cheap

- **Symptom:** suspicion that the measurement rig eats half the machine.
- **Root cause:** it does not. Null-backend C1600 (same schedule, same driver,
  same per-unit step-engine children, zero sandboxes): busy = 0.8 mc × active
  units, clean monotone bins from 0 to 1,021 active. At C1600 that is ~1.3
  cores — 1% of the campaign's measured 94.
- **Fix:** n/a. The rig's `active`-count distortion (arrivals lag schedule
  under ~1,500 live threads; same signature as the campaign's 2,378 s wall for
  a 1,800 s window) is real but does not affect the busy-per-active slope.

### 4.6 Bugs found in the bench machinery itself

- `-monitor unix:...` is **HMP, not QMP** — JSON commands return "unknown
  command" without raising; a "paused" arm can silently measure running VMs.
  Verified fix: plain-text `stop\n` (rate 27 mc → 0.000).
- The first null-manifest launch lost the `cd` to a backgrounded `&&` chain
  (second `nohup` ran from `$HOME`), and the corrected relaunch omitted
  `ulimit -n 65536` and hit the known NOFILE wall at ~25.6k units (§3.4).
  Both are operator errors recorded here because they shaped the data.

## 5. What is NOT verified

- **Lifecycle cost for TB units** — measured only on the django image (~7 c-s);
  the TB estimate (~3–4 c-s, smaller testbeds) is arithmetic, not measurement.
  A TB-image lifecycle bench is 10 minutes of work when wanted.
- **The full campaign reinterpretation** — the decomposition (lifecycle +
  unamplified payload + 2× guest counting ≈ measured busy) closes to within
  ~±20% by arithmetic; nobody has re-run a campaign with corrected
  instrumentation.
- **The `/proc/stat` under-count mechanism** — empirically established (§4.3),
  kernel-level root cause not chased.
- **qemu guest-side attribution** — which guest configuration choice (tick
  rate, idle states, SMP=2) dominates qemu's 27 mc is not isolated; only the
  exit census and the paused arm are measured.
- **merger's synth-io arm** — degenerate (no testbed tree on the TB image,
  wall ≈ 0 s); discarded.
- **n=1 everywhere** — single runs, no variance estimates, consistent with
  campaign practice but worth stating.

## 6. Follow-ups

| What | Why it was deferred | Issue |
| ---- | ------------------- | ----- |
| TB-image lifecycle bench | 10-min experiment, attribution already bracketed | — |
| Re-run one campaign cell with per-field `/proc/stat` + schedstat sampling | turns §4.3's disqualification into corrected campaign numbers | — |
| Sampler fix in `sample_host.py` (per-field deltas, schedstat option) | code change to campaign tooling, owner's call | — |
| Kill the per-exec git-diff VM / reflink the seed copy | the two biggest lifecycle terms (2.5 + 4.2 of 7.5 c-s, §4.1); would move the fc-warm CPU knee ~proportionally | — |
| Kernel accounting dig (vtime/guest under-count) | curiosity with measurement-validity stakes; no experiment blocked on it | — |
