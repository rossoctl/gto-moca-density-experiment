# Moca backend, side by side with containers and VMs — measurement report

Status: Partial <!-- pressure80 on moca was still running when this was first committed; its cells say PENDING and are filled by the dated appendix when the run lands. -->
Date: 2026-11-02
Spec: none yet — the load-generator spec is still to be written; this report is the first measured input to it.
Covers: the moca leg of the three-backend side-by-side (smoke, load50, pressure80), the per-exec cost decomposition that motivated the `sbxrun` driver, and the attribution of moca's timing stretch.

> **What this report says in one sentence.** Driving moca sandboxes through `mocactl` made every exec cost ~730 ms of client machinery; replacing the exec hot path with `tools/sbxrun` (direct sandbox/v1 gRPC, tokens minted through mocactl) drops that to ~90 ms — and with a fair driver, moca's load50 makespan stretch (+52 % median) is attributed not to the sandbox machinery but to the stateless-exec model's cold page cache (a recorded 1.85 s test step costs 4.3 s in a fresh VM, every time), while at pressure80 the baselines invert: containers stretch +40 % and VMs +168 % where moca holds [PENDING].

**Append-only.** This document is not rewritten when a later phase changes the answer — write
a new report or append a dated section. See
[the lifecycle note](../README.md#specs-revise-reports-do-not).

---

## 1. What landed

| What | Where | Notes |
| ---- | ----- | ----- |
| `sbxrun` — Go drop-in for `mocactl sbx run` | `tools/sbxrun/` | same CLI shape and exit-code semantics (command's code / 124 timeout / 125 machinery); speaks sandbox/v1 Exec gRPC directly; sandbox tokens cached on disk for their 15-min life, minted **through mocactl** so the rotating login has a single owner |
| `sbxrate` — saturation micro-benchmark | `tools/sbxrate/` | N sandboxes × M persistent workers, host-CPU accounting; used for every per-exec number below |
| `replay.py` moca backend on the new driver | `replay/replay.py` (`_moca_exec`) | prefers `~/sbx-local/sbxrun`, falls back to `mocactl` unchanged; create/rm/token stay on mocactl |
| Three-backend manifest runs | `replay/logs/manifest-{smoke,load50,pressure80}-{podman,vm,moca}/` | same manifests, byte-identical, on moca-sbx-ctr / moca-sbx-vm / moca-sbx-fc |

Hosts: three identical 32-core x86_64 machines — moca-sbx-ctr (podman), moca-sbx-vm (QEMU, `-smp 2`, snapshot-resumed), moca-sbx-fc (moca microVMs, 1 GiB guest RAM sweb templates, 1 vCPU). All moca runs used `MOCACTL_SBX_URL` pointing at moca-sbx-fc's local control plane.

## 2. Measurements

### 2.1 Per-exec cost: mocactl vs the sandbox machinery

Same sandbox (default-256), `true`, moca-sbx-fc, 2026-11-02:

| Driver | wall per exec | host CPU per exec | throughput (21 sandboxes) |
| ------ | ------------- | ----------------- | ------------------------- |
| `mocactl sbx run` (Node, per-exec process) | 730 ms | ~700 ms | 9.5 execs/s, 3,183 ms CPU/exec, 30.4 of 32 cores saturated |
| `sbxrun` one-shot (Go) | 90–190 ms | ~4 ms | — |
| `sbxrate` (persistent connection) | 83 ms p50 | 4 ms | 232 execs/s, 1.1 cores |

A `mocactl` invocation that only prints usage already costs 0.63 s CPU — Node.js process startup dominates; each exec also mints a fresh sandbox token and TLS handshake because nothing survives the process. **~99.8 % of the mocactl-driven per-exec cost was client, not machinery.** Consequence: every earlier per-exec figure measured through mocactl (including the "machinery costs ~1 s CPU/exec" estimates and the AMD-vs-Intel 3× gap) is dominated by client CPU contention and should be re-measured before being believed.

### 2.2 Manifest runs, three backends

Per-unit stretch = (done − expected)/expected over each unit's `done in …s (expected …s)` line; same manifest bytes on all three hosts.

| Manifest | Backend | units ok | median | p90 | max |
| -------- | ------- | -------- | ------ | --- | --- |
| smoke (5 units) | containers | 5/5 | — (makespan 49.1 s vs 49.2 s recorded) | | |
| | VMs | 5/5 | — (54.5 s) | | |
| | moca | 5/5 | — (~60 s) | | |
| load50 (54 units) | containers | 54/54 | **−0.6 %** | 0.5 % | 1.0 % |
| | VMs | 54/54 | **+4.3 %** | 19.0 % | 21.7 % |
| | moca | 49/54 | **+52.4 %** | 78.6 % | 136.7 % |
| pressure80 (767 units) | containers | [see logs] | **+39.8 %** | 187.6 % | 367.3 % |
| | VMs | 767 | **+167.9 %** | 698.4 % | 1279.3 % |
| | moca | PENDING | PENDING | PENDING | PENDING |

The 5 moca load50 failures are one step each (`django__django-10914`, step 44, recorded exit 0 → replay exit 1, patches still identical) in the r2/r3 reps — the same flaky-under-load step the **container** backend hit at pressure100. Not moca-specific.

### 2.3 Decomposing moca's stretch

Each candidate was measured and either convicted or acquitted:

| Hypothesis | Verdict | Evidence |
| ---------- | ------- | -------- |
| Per-exec machinery (VM restore etc.) | Acquitted | 83–87 ms p50 on both default-256 and sweb templates; 4 ms host CPU; steady-state 16-way on sweb: 87 ms p50, linear to 181.6 execs/s |
| Standby-VM pool exhaustion under steady load | Acquitted | the 16-way ladder above. (A fully **synchronized** burst of 21 cold execs does degrade to ~1.9 s each — worker-side batching — but Poisson-manifest arrivals never hit it) |
| virtiofs (workspace IO) | Acquitted | solo microbench inside an sweb sandbox: read testbed tree 0.94 s vs containers 0.10 s vs VM 2.06 s; write 128 MB/s vs 1.4 GB/s vs 102 MB/s — mid-pack, not 25 %+ |
| Memory pressure | Acquitted | 6.4 of 62 GiB used at load50 peak; live VMs bounded by in-flight execs, not sandbox count |
| vCPU count | Contributing | firecracker VMs have **1 vCPU** (`nproc` = 1 in-guest; QEMU backend uses `-smp 2`, containers the whole host). Visible as CPU time: runtests user 1.34 s in moca vs 1.01 s in container |
| **Cold page cache per exec** | **Convicted (dominant)** | recorded 1.85 s `runtests.py` step: container warm **1.63 s**, moca fresh VM **4.29 s** — and **4.26 s again on the very next exec**, because every exec boots a fresh VM and the guest page cache never survives. A trajectory's ~10 test steps × ~2.4 s extra + ~25 short steps × ~0.5 s machinery ≈ the observed 40–66 s slides |

Root cause in one line: **the recorded timings come from warm, long-lived containers; moca's stateless-exec model pays cold Python/Django import on every step, and the VM backend escapes only because its VM is long-lived across steps.**

### 2.4 Fix candidate (not yet tried)

Guest page cache lives in guest RAM, and the firecracker snapshot captures RAM. If the sweb templates' prep stage warmed the caches (import Django, touch the test tree) *before* snapshotting, restored VMs would start warm and the cold-cache term should collapse toward the VM backend's numbers. This is a template-prep change in the image pipeline. The 1-vCPU setting is a worker config knob (`SH_…`), worth an A/B once the warm snapshot exists.

## 3. Deltas from the design

- `_moca_exec` now prefers `sbxrun` — a behavior change to the replay driver, landed with the mocactl fallback so old hosts keep working.
- `MOCACTL_SBX_URL` must be exported on the host running the moca backend (mocactl does not fall back to the auth file's remembered URL for `sbx create` on these hosts).

## 4. Incidents found and fixed along the way

All three were found by this work and are fixed in `tools/sbxrun` (see git history):

1. **req_id = 0**: the Exec protocol dedups in-flight execs by `req_id`; a fixed 0 makes concurrent execs on one sandbox refuse each other (`req_id_in_flight`). Fixed: 24-bit process salt + 8-bit counter — also kept under 2^53, above which the Node.js relay silently loses the value and every exec dies before the audit log.
2. **Trailers-only gRPC errors**: the relay's per-sandbox in-flight cap (2) arrives as `grpc-status` in the response *headers*; reading only trailers reports a bare stream EOF. Fixed: surfaced as exit 125 "sandbox busy", which `_moca_exec`'s existing retry handles.
3. **Rotating refresh token, two owners**: `sbx-auth.json` is shared state and its refresh token rotates on every refresh; two clients refreshing concurrently presents a superseded token, which the control plane reads as theft and revokes the family (audit: `refresh_reuse_detected`, then 192 `revoked` refusals during load50's tail). Two logins were lost to this. Fixed: `sbxrun` no longer talks to the control plane — mocactl is the single owner of the login.

## 5. What is NOT verified

- **moca pressure80** — PENDING in §2.2; appendix will follow when the run completes.
- The warm-snapshot fix (§2.4) is a hypothesis, not a measurement.
- The AMD-vs-Intel per-exec gap and the DeferReapWorkers / SH_WORKSPACE_IMAGE_MB "no effect" results from the earlier mocactl-driven experiments were never re-measured with the fair driver; they are probably client-CPU artifacts but that is unproven.
- Only one run per manifest per backend; no variance estimate.
- The moca pressure runs use manifests whose initial fill (80 units at t = 0) is fully synchronized — known to hit the worker's burst-batch behavior (~1.9 s for a cold synchronized wave). The container/VM baselines ran the same bytes, so the comparison is fair, but moca's absolute numbers carry this caveat.
- Resource-per-unit plateaus (the density experiment's actual target metric) are not measured here at all; this report covers timing fidelity only.

## 6. Reproducing

```sh
# per-exec machinery (any host with the local moca stack):
~/sbx-local/sbxrate -addr <exec-endpoint> -ca <exec-ca> -token <token> \
    -sid <worker-id> -wsk <sbx-id> -workers 1 -window 15s

# manifest legs (on each backend host):
cd replay && MOCACTL_SBX_URL=http://<cp>:8080 python3 replay_manifest.py \
    manifests/load50.json --backend moca   # or podman / vm
```

Per-unit stretch: `grep -h 'done in' logs/manifest-<name>-<backend>/*.log`, stretch = done/expected − 1.
