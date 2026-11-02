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

---

## Appendix — 2026-11-02: pressure80 on moca (the PENDING cells)

The run completed enough to fill §2.2's PENDING row. Outcome: **moca does not complete pressure80; it hits two administrative capacity ceilings and fails 92 % of units**, where the same manifest bytes stretched containers +40 % and VMs +168 % but completed.

| pressure80 (767 units) | units ok | median | p90 | max |
| ---------------------- | -------- | ------ | --- | --- |
| containers | 767/767 | +39.8 % | +187.6 % | +367.3 % |
| VMs | 767/767 | +167.9 % | +698.4 % | +1279.3 % |
| **moca** | **60 completed, 0 passed cleanly out of 767** | **+1239.9 %** (of the 60 that ran) | +1730.3 % | +2233.7 % |

Unit outcomes on moca:

| count | outcome |
| ----- | ------- |
| 481 | create refused — control plane at capacity (`SH_SANDBOX_MAX_TOTAL`/`MAX_PER_SUBJECT` = 150 live sandboxes) |
| 189 | seed refused — worker committed-memory budget (`MaxCommittedBytes` 39.8 GiB ⇒ ~36 concurrent 1-GiB sweb VMs); retry backoff (5 tries, 77 s) could not outlast the saturation |
| 60 | completed the replay; median stretch **+1240 %** — serialized behind the memory budget, each unit queuing for VM slots |
| 37 | killed mid-run in the drain tail (operator decision: "not worth waiting"; the last xarray units were still queued) |

Reading: at 80-way pressure the three backends separate into *silent degradation* (containers), *loud degradation* (VMs, 13× stretch at p90) and *refusal* (moca). The moca behavior is configured, not fundamental: 80 × 1.1 GiB = 88 GiB of concurrent VMs cannot fit this 62-GiB host regardless of config, but the 150-sandbox and 39.8-GiB budget numbers are policy choices, and the retry-then-fail (rather than retry-forever) behavior is the driver's. What this run does establish: the ceilings are enforced cleanly (pre-exec refusals, no leaked state once cleanup runs), the refusal texts are precise enough to attribute every failure, and the units that did run stretched ~13× — the memory budget serializes rather than sheds load.

Three driver bugs were found by the two failed attempts before this one (both fixed, see `git log`): `sbxrun`'s RPC deadline was hardcoded below the command timeout (long seeds died as `Deadline exceeded`); `mocactl sbx token --json` does not carry the exec route (it comes from `sbx env`); and `_moca_exec` retried only the "template busy" refusal, not the documented `memory-budget:` / `queue full` / `max-runs:` pre-exec refusals.

Caveats specific to this appendix: the 37 killed units have no terminal state and are excluded from stretch statistics; the 60 completions' +1240 % median is measured on a survivor-biased subset (only units that got under the caps early); load50's leaked sandboxes (cleaned up before this run) were the collateral of the login-revocation incident in §4.3, and operational rule "list live sandboxes before a moca manifest run" comes from it.

---

## Appendix — 2026-11-02: the cold-cache experiment (what §2.3's "cold page cache" actually is)

§2.4 hypothesized the cold cost was Python imports. Measured directly (one exec per scenario = one fresh VM, benchmark repeated within each exec), on **both** sweb-django-10914 (`runtests.py file_uploads file_storage`, container baseline 1.85 s) and sweb-xarray-4094 (`pytest -k stack`, container baseline 5.29 s):

| scenario | django first run | xarray first run | warm reference |
| -------- | ---------------- | ---------------- | -------------- |
| no warmup (cold) | 4.30 s (sys 1.33) | 14.41 s (sys 3.03) | 2.97 s / 8.36 s |
| cat all `.py`/`.pyc` under miniconda+testbed | 4.14 s | 11.71 s | " |
| `import django` / `import xarray,pandas,numpy` | 4.19 s | 10.58 s | " |
| guest `drop_caches` after a warm run | 3.29 s | 10.49 s | " |
| **cat the entire rootfs** | **3.08 s** | **9.12 s** | " |

Findings, in order of consequence:

1. **The cold set is the whole rootfs, not the Python trees.** Warming only `.py`/`.pyc` (or running the imports) recovers little on django and only partially on xarray (where the `.so` libraries numpy/pandas load are large); reading *every file* recovers essentially everything, on both templates. The fix candidate of §2.4 survives but in its dumbest form: **template prep can be a whole-rootfs read pass** (~13–19 s, template-agnostic, no knowledge of the workload) — or simply running one representative test command before snapshotting. Warmth lives in guest RAM, which the firecracker snapshot captures.
2. **`drop_caches` restores only ~25 % of the cold cost** — most of the penalty is not re-readable guest pages but the read/fault path itself (virtiofs serves guest reads through the host). Consistent with S2-vs-S5: what matters is that the bytes have been *read once* in that VM.
3. **A second, separate penalty survives full warmth**: django 2.97 s vs 1.63 s container (+82 %), xarray 8.36 s vs 5.29 s (+58 %) — dominated by **user**-time (django: 1.31 s vs 0.74 s), i.e. pure CPU, not IO. Prime suspect is the 1-vCPU firecracker configuration (or its CPU model); that is a worker-config A/B, not a cache problem, and it bounds what warm snapshots alone can recover.
4. Minor artifact: xarray's import emits a matplotlib temp-dir warning every exec (unwritable `/.config`) — a small recurring stateless cost, fixable by pointing `MPLCONFIGDIR` at the persistent workspace.

So the load50 stretch (+52 % median) decomposes into: cold-rootfs cache (recoverable by warm snapshots, worth ~1.3 s/step on django, ~6 s/step on xarray) **plus** a warm CPU penalty (~+58–82 % on test steps, config question) — and the arithmetic of §2.3 attributed too much to the former.

Not verified: whether a warm snapshot actually transfers this warmth (the read pass was per-VM, the snapshot mechanism untested); the cause of the user-time penalty (1 vCPU vs CPU model not distinguished); container baselines are recorded timings, not re-measured today.
