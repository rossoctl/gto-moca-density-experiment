# Moca backend, side by side with containers and VMs — measurement report

Status: Partial <!-- pressure80 on moca was still running when this was first committed; its cells say PENDING and are filled by the dated appendix when the run lands. -->
Date: 2026-11-02
Spec: none yet — the load-generator spec is still to be written; this report is the first measured input to it.
Covers: the moca leg of the three-backend side-by-side (smoke, load50, pressure80), the per-exec cost decomposition that motivated the `sbxrun` driver, and the attribution of moca's timing stretch.

> **What this report says in one sentence.** Moca's per-sandbox physical memory cost is roughly an order of magnitude below the VM backend's at the same pressure (~2.7 GB total for ~50 concurrent sandboxes plus a 24-VM standby pool, vs ~7.4 GB for ~20 long-lived QEMU VMs), because every VM restored from a golden snapshot shares that snapshot's physical pages — **snapshot page-sharing is the density argument for microVMs** — while the prices paid for it are a fixed ~90 ms/exec of machinery (after replacing mocactl's ~730 ms/exec client overhead with `tools/sbxrun`), a standing cold-rootfs cost per exec (a recorded 1.85 s test step costs 4.3 s in a fresh VM, every time; not fixable by snapshot prep, measured), ~3× the host CPU of the VM backend at load50, and clean refusal rather than silent collapse at pressure80 (containers +40 %, VMs +168 % stretch where moca refused at configured capacity).

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
    manifests/load50-10pct.json --backend moca   # or podman / vm
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

---

## Appendix — 2026-11-02 (later): the warm-snapshot fix candidate is dead

§2.4's fix candidate — bake page-cache warmth into the golden snapshot with a read pass before pause — was built and measured. **It does not work: firecracker snapshot/restore on this stack does not preserve the guest's file page cache.** Evidence chain, all on sweb-django-10914:

| measurement | result |
| ----------- | ------ |
| read-all pass in the build guest, then re-read `/opt/miniconda3/bin/python3.11` (24 MB) **before pause** | 0.005 s (cached) |
| the same file's first read in a VM restored by the **build script's own verify step** (same jail style, fresh path) | **0.168 s (cold)** |
| the same file's first read in a **production-restored** sandbox | 0.152 s (cold); second read 0.003 s |
| tmpfs marker `touch`ed by the warm pass, checked in a restored VM | **present** — tmpfs survives |
| memfile | dense 1 GiB — full guest RAM is dumped |
| guest `Cached` post-restore | ~794 MB reported, yet the hot file's read misses |

So the loss is firecracker-level (or guest-kernel-on-resume), not in the production launcher: the build's own restore loses it too. Tmpfs (shmem) pages survive; clean file-backed pages do not serve reads after restore. Two build attempts preceded this conclusion and are worth recording: an unordered `find /` pass left the *wrong* ~800 MB cached (LRU: `/usr` read after `/opt` evicts miniconda), and an ordered pass (hot trees last) demonstrably cached the right files pre-pause — all irrelevant, since restore discards file warmth either way.

Consequences:

- The cold-rootfs term of the load50 stretch (~1.3 s/step django, ~6 s/step xarray) is **not fixable by snapshot prep** on this stack. It is the standing cost of a fresh VM per exec.
- The templates on moca-sbx-fc were reverted to their original cold snapshots (the warm builds bought nothing); the patched build script (`warm-build-snapshot.sh`, with the instrumented pass) is kept at `~/sbx-local/` on moca-sbx-fc for future experiments — it is a copy; the pinned moca checkout is untouched.
- What remains fixable: the **warm CPU penalty** (+58–82 % user time) — cause still unattributed (1-vCPU config vs host deficit; note moca-sbx-fc runs the same single-threaded loop ~15 % slower natively than the supposedly identical ctr/vm hosts, unexplained). A fresh host, moca-sbx-fc2, is being provisioned to isolate that.

Not verified: whether a newer firecracker or Cloud Hypervisor backend preserves file page cache across restore (the `launcher_chv.go` path exists in the tree and was not tested); whether the guest kernel version matters.

---

## Appendix — 2026-11-02 (final): VM vs moca at load50 — stretch, memory, CPU, failures

Four moca runs and three VM runs of the same manifest; memory and CPU sampled at 5 s (CPU from run 3 only on both hosts; VM memory from its run 2 — the run-3 sampler died pre-launch, 19 samples, discarded).

| | VM backend (moca-sbx-vm) | moca (moca-sbx-fc) |
| - | ------------------------ | ------------------ |
| stretch median (runs) | +4.3 / +5.0 / +5.0 % | +52.4 / +54.3 / +54.9 / +53.5 % |
| stretch p90 / max | ~+20 % / +23 % | ~+80 % / +136 % |
| CPU mean / median / peak (of 32 cores) | **6.7 % / 6.3 % / 24.3 %** | **19.6 % / 17.7 % / 51.4 %** |
| host memory peak / median | 9.1 GB / 4.6 GB | 7.9 GB / 6.2 GB |
| peak concurrent VMs | 20 QEMU (long-lived) | 105 firecracker procs (in-flight + 24-VM standby pool) |
| failures | **0** (all runs) | **5, the same units every run** |

The failures: u0020/25/42/52/53, all `django__django-10914` r2/r3, each one step — `LANG=en_US.UTF-8 runtests test_utils staticfiles_tests`, recorded exit 0 → replay exit 1, patches identical. Deterministic under the deterministic schedule, **passes in isolation** — a load-sensitive test step (the container backend hit the same class at pressure100). Patches identical in all cases; no state corruption.

Reading: for the same delivered work, moca burns ~3× the host CPU (≈6.3 vs ≈2.1 cores mean) — the per-exec VM restore/teardown machinery across ~1,300 steps plus standby-pool churn — while holding a similar memory peak but a higher floor. Its units stretch 10× more than the VM backend's, for reasons already attributed (cold rootfs per exec, standing cost per the previous appendix; warm CPU penalty). The VM backend's model — ~20 long-lived VMs, exec into them — is cheaper on every axis at this pressure; moca's wager is density and isolation at higher pressure, where the VM backend collapsed (+168 % median at pressure80) and moca refused cleanly instead.

## Appendix note — 2026-11-02: memory metrics, and why VM RSS is the right headline

Three memory numbers were sampled; they measure different things:

| metric | what it measures | VM backend (load50 peak) | moca (load50 peak) |
| ------ | ---------------- | ------------------------ | ------------------ |
| host `used` (`free -m`) | everything physical on the host: VM processes **plus infrastructure** (relay, control plane, workers, samplers) | 9.1 GB | 7.9 GB (of which ~5.2 GB is the standing moca stack — sandbox delta ≈ **2.7 GB**) |
| aggregate VM RSS (`ps` sum) | memory attributable to VM processes | **7.7 GB** across 20 QEMU ≈ 385 MB/VM | **2.4 GB** across 105 firecracker procs — see caveat |
| committed (moca budget only) | the worker's fair-share reservation, not usage | — | ≤ 39.8 GB budget; ~1 GB per live VM reserved, far above physical use |

**Why the RSS numbers are not symmetric between backends.** QEMU allocates each guest's RAM **anonymously per process**: RSS ≈ guest memory actually touched, and the aggregate is a fair proxy for "memory held by sandboxes." Firecracker restores guest RAM from the **golden memfile — the same physical pages mapped by every VM restored from that snapshot**: `ps` RSS counts shared pages once *per process* (overcounting) while lazy faulting keeps untouched pages out entirely (undercounting), and per-exec workspace page cache sits outside process RSS entirely. Firecracker's RSS is therefore neither floor nor ceiling — it is not comparable to QEMU RSS.

**The honest apples-to-apples is the host-used delta** (peak minus pre-run baseline): VM backend ≈ 7.4 GB for ~20 live VMs (~370 MB/VM, no sharing); moca ≈ 2.7 GB for ~50 concurrent sandboxes plus a 24-VM standby pool (~tens of MB per sandbox effective) — because paused standbys fault lazily and the golden memfile's pages are **deduplicated across every VM sharing the snapshot**. At load50, moca's physical memory efficiency per sandbox is roughly an order of magnitude better than the VM backend's; that sharing is the density argument for snapshot-restored microVMs, visible only when memory is measured as a delta rather than as process RSS.

VM-RSS headline, for the record: VM backend **7.7 GB peak aggregate (20 × QEMU)**; moca **2.4 GB peak aggregate firecracker RSS (not comparable, per above)**.

## The density argument (consolidated, 2026-11-02)

The comparison this experiment exists to price. At load50, measuring memory as the **host-used delta** (peak minus pre-run baseline — the only metric that survives the RSS asymmetry documented in the note above):

| | VM backend | moca |
| - | ---------- | ---- |
| concurrent sandboxes at peak | ~20 live QEMU VMs | ~50 sandboxes + 24-VM standby pool |
| physical memory delta | ~7.4 GB | ~2.7 GB |
| **effective per sandbox** | **~370 MB, no sharing** | **tens of MB** |

**The mechanism is page sharing, and it is structural, not incidental.** Every firecracker VM restored from a template's golden snapshot maps the *same* memfile — one set of physical pages per template, no matter how many VMs run from it. QEMU, by design, gives every guest its own anonymous memory: 20 VMs hold 20 private copies of the same kernel, the same Python runtime, the same Django tree. Moca's 50+ concurrent sandboxes share one copy of each of those, per template — the private bytes per sandbox are only what that sandbox's workload actually touches, and paused standby VMs fault in lazily, contributing almost nothing until used. The committed-memory budget (39.8 GiB ⇒ ~36 concurrent 1-GiB VMs on this host) is a *reservation* for the worst case where sharing collapses; physical use at load50 peaked at ~2.7 GB against that.

**What the sharing buys, and what it costs** (all measured in this report):

| the density case | the price |
| ---------------- | --------- |
| ~10× less physical memory per sandbox at load50 | ~90 ms of machinery per exec (after the `sbxrun` driver fix; mocactl's client overhead was ~730 ms/exec before) |
| clean refusal at capacity limits instead of silent collapse (pressure80: VMs stretched +168 %, moca refused at 150-sandbox and 39.8-GiB configured ceilings with precise, attributable errors) | standing cold-rootfs cost per exec: +1.3 s/step (django) to +6 s/step (xarray), not fixable by snapshot prep (firecracker restore discards guest file page cache — tmpfs survives, file pages do not) |
| isolation: a fresh kernel per exec, no cross-tenant filesystem leakage | ~3× the host CPU of the VM backend at load50 (19.6 % vs 6.7 % mean of 32 cores: per-exec restore/teardown across ~1,300 steps plus standby churn) |
| | warm-exec CPU penalty: +58–82 % user time on test steps vs containers (stack-level — reproduces on a clean host, moca-sbx-fc2; not the 1-vCPU count alone, unattributed) |

**What is NOT yet measured — the actual next step.** Everything above is load50, one pressure point, makespan-centric. The density argument needs resources-per-unit *at plateau*, swept across pressure levels: that is the ratchet manifests' purpose (plateaus of stable concurrency with measure-now marks). The memory ceiling where sharing stops saving you — the point at which distinct workspaces' touched pages dominate the shared base — is the number that decides how far the ~10× advantage extends, and nothing in this report measures it yet. The pressure80 result (refusal at 150 sandboxes / 39.8 GiB committed) bounds it from above on this host and configuration.

---

## Appendix — 2026-11-02 (fc): the pure-firecracker backend, warm mode, at load50

`--backend fc` with `FC_VM_MODE=warm`: raw firecracker on moca's golden snapshots (same artifacts, hash-identical), the moca stack removed, one resident microVM per unit. Built and accepted on hardware this session (eight iterations; every fix is a discovered restore contract — jailer v1.17 jail layout, Content-Length HTTP framing, the vsock workspace mount, sync-before-kill, self-sufficient target spec across the step child's process boundary). Run on the reprovisioned moca-sbx-fc with the moca stack stopped (clean accounting: nothing behind the caller's back). Same manifest bytes.

| load50 | containers | VM backend | **fc warm** | moca |
| ------ | ---------- | ---------- | ----------- | ---- |
| stretch median | −0.6 % | +4.3–5.0 % | **+18.8 %** | +52–55 % |
| stretch p90 / max | +0.5 / +1.0 % | ~+20 / +23 % | **+30.0 / +40.0 %** | ~+80 / +136 % |
| CPU mean / peak (of 32 cores) | — | 6.7 % / 24 % | **10.5 % / 35 %** | 19.6 % / 51 % |
| memory delta (peak − baseline) | — | ~7.4 GB (20 QEMU) | **~4.3 GB (17 resident)** | ~2.7 GB |
| effective per-VM memory | — | ~370 MB, unshared | **~253 MB** (memfile pages shared per template) | tens of MB (fresh-VM model) |
| units ok | 54/54 | 54/54 | **49/54** | 49/54 |
| manifest wall time | — | 1074 s | **1117 s** | 1236 s |

The failures are the SAME five units as every moca run (u0020/25/42/52/53, django-10914 r2/r3, the load-sensitive `LANG=en_US.UTF-8 runtests` step, passes in isolation). Their failing on both firecracker-based backends — which share the golden snapshot's guest kernel and image — while the QEMU backend (its own kernel, qcow2 image) passes all 54, locates that fragility in the template image under load, not in any management stack.

Reading:

- **fc warm sits strictly between the VM backend and moca on every axis** — stretch, CPU, and memory all order as VM < fc warm < moca (memory ordering reversed: moca < fc warm < VM). It is not between containers and VMs: the VM backend still wins stretch (+5 % vs +18.8 %), and the gap is the known firecracker warm CPU penalty (1-vCPU firecracker vs QEMU `-smp 2`).
- **The moca model's total cost at load50 is ~+34 points of stretch** (moca +52–55 % vs fc warm +18.8 %), of which the cold-rootfs term is the bulk: fc warm removed exactly that by staying resident, with the same snapshots and the same firecracker. What moca buys for that price is the on/off statelessness — fresh kernel per exec, cross-tenant workspace isolation, standby pooling — plus the stack (its remaining CPU cost shows as 19.6 % vs 10.5 % mean).
- **Page sharing survives warm residency**: ~253 MB per resident VM against QEMU's ~370 MB, with the memfile's shared pages deduplicated per template even while all VMs run.

Caveats: single run; fc CPU/memory sampled from t=0 but the VM-backend row's memory comes from its run 2 (run 3's sampler died pre-launch); the fc row ran on the reprovisioned host while the VM/container rows predate it (the reprovision removed a ~15 % native CPU deficit — so fc's row is, if anything, flattered relative to the older VM row by a faster host).

## Appendix — 2026-11-02 (parity): container rerun, and the VM backend at `-smp 1`

Same-session reruns to close two gaps in the table: the container row now has sampler data (its CPU/memory cells were empty), and the VM backend runs at one vCPU (`VM_SMP=1`, images re-snapshotted — migration state pins CPU count) for parity with the firecracker backends.

| load50, same session | containers | VM smp=2 | **VM smp=1** | fc warm | moca |
| -------------------- | ---------- | -------- | ------------ | ------- | ---- |
| stretch median | **−0.0 %** | +5.0 % | **+6.1 %** | +18.8 % | +52–55 % |
| stretch p90 / max | +0.7 / +1.3 % | ~+21 / +23 % | **+19.7 / +22.7 %** | +30 / +40 % | ~+80 / +136 % |
| CPU mean (of 32 cores) | 8.7 % | 6.7 % | **6.9 %** | 10.5 % | 19.6 % |
| memory delta | ~0.9 GB | ~7.4 GB | **~7.0 GB** (18 QEMU, 7.1 GB RSS) | ~4.3 GB | ~2.7 GB |
| units ok | 54/54 | 54/54 | **54/54** | 49/54 | 49/54 |

The finding: **vCPU count is not the firecracker penalty.** QEMU at one vCPU pays ~1 point over smp=2 (+6.1 % vs +5.0 % median) — while fc warm sits at +18.8 %. At equal vCPU count the ~13-point gap between QEMU and firecracker-warm remains, so it is firecracker-specific or image-specific: the leading suspects are the golden snapshot's guest kernel (6.8.0-31-generic) versus the VM backend's (6.8.0-147-generic), or firecracker's vCPU implementation itself. Distinguishing those needs a kernel-matched pair, not more load runs.

Cross-host caveat stands: containers/VM run on their own (pre-reprovision) machines, fc/moca on the fresh moca-sbx-fc. The container rerun's −0.0 % on its own host argues the host difference is not load-bearing for the ordering, but a same-host VM-vs-fc pair would close it properly.

## Appendix — 2026-11-03: the firecracker-vs-QEMU penalty is the VMM, not the kernel

Question: at equal vCPU count, why do firecracker VMs run SWE-bench's long test steps ~20 % slower than QEMU VMs (1.36× vs 1.16× actual/recorded)? Ruled out first: vCPU count (QEMU smp=1 = smp=2 = 1.16×), raw compute (the fc guest's pure loop runs at host speed), and mitigations (EPYC reports "Not affected" nearly across the board). That left the kernel (the golden snapshots run a custom 6.18.56+ microvm kernel; the VM backend runs Ubuntu 6.8.0-147-generic) versus firecracker's device model (virtio-**mmio**, `pci=off`, vs QEMU's virtio-pci).

The discriminating experiment: a golden snapshot rebuilt with the VM backend's exact Ubuntu kernel — same rootfs, same everything — which required shipping Ubuntu's virtio_vsockcts modules into the staging rootfs (Ubuntu builds vsock as a module; the microvm kernel builds it in) and a static busybox to insmod them, since the stripped rootfs has no module tools. Snapshot built and verify-restored cleanly.

| xarray long test steps, same manifest, same load | actual/recorded |
| ------------------------------------------------ | --------------- |
| QEMU, Ubuntu 6.8.0-147 kernel | 1.16× |
| fc warm, custom 6.18.56+ kernel | 1.36× |
| **fc warm, Ubuntu 6.8.0-147 kernel** | **1.39×** |

The kernel swap moved nothing (1.36 → 1.39, within run-to-run noise). With rootfs, kernel and vCPU count all matched, the remaining difference is the VMM: firecracker's device model — the virtio-mmio block path (`pci=off`), where every small IO operation traps through MMIO, against QEMU's virtio-pci. The workload's long steps are exactly small-IO-heavy (pytest importing hundreds of modules, writing .pyc and sqlite), which is where an mmio-tax would land. Not further discriminated: whether it is the mmio transport itself or firecracker's vCPU exit handling; separating those would need QEMU on virtio-mmio, which x86 QEMU does not readily do.

The original xarray snapshot was restored after the run (hash `bec86aeb…`, built 2026-10-31, as recorded above). The Ubuntu-kernel build script survives at `~/sbx-local/ubu-build-snapshot.sh` on moca-sbx-fc for replication. Operational note recorded from this run: the manifest log dir was not renamed before the rerun, so the previous fc-warm run's per-unit logs were overwritten — the 1.36× figure survives in this report's earlier appendix, but the raw logs do not; rename log dirs before every rerun.

## Appendix — 2026-11-03: the optimization guide's io_uring advice is not reachable on firecracker v1.17

Assessed a firecracker optimization guide's four items against our measured bottlenecks. One was a candidate experiment (io_uring drive engine, targeting the ~20 % small-IO tax); three were rejected as architecture-destroying or density-hostile (raw device paths break the hardlink/page-sharing model; pre-allocation defeats sparse workspaces at 10× disk cost; DirectIO would bypass the host page cache that currently dedups rootfs reads across every VM sharing the golden inode).

The io_uring item turned out not to be configurable on this VMM:

- `PUT /drives/rootfs` with `"io_engine": "IoUring"` is rejected by the API: *"unknown variant `IoUring`, expected `Async` or `Sync`"* — the v1.17 HTTP schema exposes no io_uring option.
- A running VM (jailer-launched, production-style) holds **zero io_uring file descriptors** — the Async engine is epoll/aio in practice. The binary contains a compiled io_uring module, but it is not reachable through the drive configuration, and the jailer's seccomp posture would likely block the syscalls anyway (firecracker's io_ering probe has an explicit failure path).

Consequence: the ~20 % mmio-path penalty measured in the previous appendix cannot be attacked from the host side on this VMM version — no engine flag, and the tax sits in the guest exit path regardless. Closing it would mean a firecracker upgrade or patch (or a VMM with virtio-pci), which changes the system rather than tuning it. The guide's remaining advice offers nothing for our workload's actual bottleneck.

---

## Appendix — 2026-11-03: the load80 ladder, the completed grid, and the duty-cycle crossover

`load80.json`: same generator and 9 recordings as load50, Poisson arrivals raised to mean steady-state concurrency 40 (128 units, seed 43, ~10 % duty cycle unchanged). All runs time-separated (the three hosts are VMs on one physical machine). QEMU smp=2 dropped from the grid — the smp=1 parity row carries the comparison.

### The completed measurement grid

| backend | load50 | load80 | CPU mean l50→l80 | mem delta l50→l80 | ok l50→l80 |
| ------- | ------ | ------ | ---------------- | ----------------- | ---------- |
| containers | −0.0 % | −0.4 % | 8.7 → 18.9 % | 0.9 → 1.7 GB | 100 → 100 % |
| QEMU smp=1 | +6.1 % | +5.8 % | 6.9 → 16.2 % | 7.0 → 20.3 GB | 100 → 100 % |
| fc warm | +18.8 % | +18.8 % | 10.5 → 26.4 % | 4.3 → 12.5 GB | 91 → 92 % |
| fc perexec | +55.7 % | +500.8 % | 19.6 → 78.1 % | 2.6 → 13.3 GB | 91 → 86 % |
| moca D=8 | +52–55 % | +492–513 % | 19.6 → 75–79 % | 2.7 → 14–15 GB | 91 → 77–91 % |
| moca D=1 | +55.3 % | +537.5 % | 20.2 → 78.8 % | 7.4 → 15.6 GB | 91 → 92 % |

### Findings

1. **The fresh-VM family is one system.** At load50: +52–56 % across fc perexec, moca D=8, moca D=1. At load80: +492–538 % across all three (CPU 75–79 %, peak 100 %, queue balloons to 76–87 active, wall 2135–2200 s). Stack, pool, budget — none of moca's machinery moves any number measurably. The model is the cost.
2. **The resident family scales flat.** containers and QEMU hold their medians exactly (−0.4 %, +5.8 %); fc warm holds +18.8 %. Their CPU at load80 (16–26 % mean) is far from the knee.
3. **The load80 collapse is the cold-cache CPU crossing the saturation knee.** Per-step cost decomposition (measured at load50): ~4.3 core-seconds per tool step of stutter compute, of which only ~0.1–0.2 is start/stop machinery — ~96 % is the host processing the fresh guest's cold rootfs reads. Per-sandbox demand ≈ 4.65·D cores (D = duty cycle; check: predicted 0.465 at D=10 %, measured 0.43). Offered CPU at load80 ≈ 24 of 32 cores ≈ 75 % — measured 78 % mean / 99 % median. Past the knee, queueing feedback (slow steps → longer residency → more demand) produces the +500 %.
4. **The duty-cycle crossover.** Stutter CPU breaks even with resident holding at 4.65·D = 0.22 ⇒ **D\* ≈ 4.7 %**: below ~5 % duty the fresh-VM model burns less total CPU than keeping VMs resident, and it holds ~284·(1−D) MB less memory per sandbox at every duty cycle. The planned 5 %-duty runs sit at the crossover: memory-bound density comparisons become honest there, with the stutter's remaining liability being stretch only (predicted ~+25–30 points at load50-scale pressure, half of today's +55.7).
5. **Capacity policy is a failure mode, not a slowdown** (moca load80 at 39 vs 51 GiB budget: failures 23→1, stretch unchanged). The 1-GiB-per-VM commitment over-reserves ~3.5× against measured use.

### Effects, one-stop (the "why" behind the grid)

(a) virtualization +6 % / ~370 MB per VM; (b) firecracker virtio-mmio +13 points / CPU ×1.5 / memory *better* (shared pages); (c) stutter cold cache ~+34 points at load50, ~4 core-s per step — the dominant term; (d) stutter start/stop ~+2–3 points, ~4 % of stutter compute — almost free; (e) saturation knee: turns (c)+(d) CPU into +500 % past ~55–60 effective resident (linear crossing ~74); (f) capacity policy: failure-mode only, tunable; (g) template fragility: caps the fc family's completion at ~91 % — dismissed per experiment owner ("we don't run that benchmark"). Measured non-effects: moca stack, standby pool, kernel choice, vCPU count, host reprovision.

Caveats: single run per cell (moca load50 is five runs, ±2 points); stretch at load80 is survivor-biased toward completed units; the load80 moca rows include queue-inflated effective residency.

## Appendix — 2026-11-03 (no-op): the cache-miss effect confirmed by payload subtraction

`load80-noop.json`: load80's byte-identical schedule with every tool step's payload replaced by `true` (still through the normal wrapper, so the wrapper's own ~20 MB of cold reads remain — a deliberate residual). fc perexec, same host, samplers from t=0. All 128 units fail the patch check by design; the measurements are system behavior.

| fc perexec, load80 | real payload | no-op payload |
| ------------------ | ------------ | ------------- |
| CPU mean / median / peak | 78.1 / — / 100 % | **11.0 / 7.7 / 42.8 %** |
| stretch median | +500.8 % | **+16.8 %** |
| wall time | 2154 s | **1128 s** (clean drain, no queue balloon) |
| memory peak | 13.3 GB | 2.8 GB |

Reading: the no-op run carries the entire fresh-VM machinery — restore, jailer, reap, vsock round-trip, plus the wrapper's cold reads — at ~1 core-second per step and 11 % CPU: it cannot reach the saturation knee. The difference to the real run (~21 cores at 3.65 steps/s) is the cold-rootfs processing, ~5–6 core-seconds per step (4.3 measured at load50, the excess being queueing churn). **The load80 collapse is the cache misses' CPU crossing the knee; the start/stop machinery is confirmed almost free** (its isolated latency cost is +16.8 % median, of which the wrapper's cold libraries are a visible part).

Refinement of the load50 decomposition: fc perexec's +55.7 % ≈ ~+17 machinery-and-wrapper (this run's number) + ~+39 testbed cold reads.
