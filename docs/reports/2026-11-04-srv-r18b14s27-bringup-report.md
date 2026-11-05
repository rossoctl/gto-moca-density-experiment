# srv-r18b14s27 bring-up — all three sandbox backends smoke-verified

Status: Complete
Date: 2026-11-04
Spec: n/a — host bring-up, not a spec'd slice
Covers: provisioning of srv-r18b14s27 and first-principles builds of the podman, qemu and firecracker backends; smoke-manifest verification of each

> **What this report says in one sentence.** The new 192-core workstation runs all three
> backends green on the smoke manifest (5/5 units, patches byte-identical, makespan within
> 1s of recorded), entirely from artifacts built on that host — and the bring-up flushed out
> three real host-portability bugs (dracut initrd, qemu 10.x migration-status wording,
> state.bin ownership), all fixed on `main`.

**Append-only.** This document is not rewritten when a later phase changes the answer — write
a new report or append a dated section. See
[the lifecycle note](../README.md#specs-revise-reports-do-not).

---

## 1. What landed

| Piece | What it is | Where |
| ----- | ---------- | ----- |
| Host provisioning | Ubuntu 26.04.1, kernel 7.0.0-42-generic, key auth + passwordless sudo + kvm group, emacs, go 1.26.8, podman 5.7.0, qemu 10.2.1, firecracker/jailer v1.17.0 | the host |
| Repo + moca checkouts | read-only https clone; moca @ `38aa2146` | `~/repo/gto-moca-density-experiment`, `~/repo/rossoctl/moca` |
| Container images | 3 swebench testbeds built from Dockerfiles (digest-pinned docker.io bases) | rootless podman store |
| VM images | qcow2 + `state.bin` + host kernel triplet (7.0.0-42, slim dracut initrd) | `images/<instance>/vm/` |
| moca templates | `moca-sbx-sweb-*` images (pinned remote-worker, rg 15.2.0, gh 2.63.2), rootfs trees, verified golden snapshots (FC CI kernel 6.18.56, 1 GiB guest RAM) | `/srv/sbx/snapshots/sweb-*` |
| fc backend prep | `replay/fc-guest-client` built inside the pinned moca checkout | repo |

## 2. Measurements

Smoke manifest = `replay/manifests/smoke.json` (5 units, `django__django-11133` r1,
window [0, 150]s, ~49.2s service per unit, peak concurrency ~2), one run per backend on
2026-11-04. PASS criteria: all tool steps replay ok, replayed patch byte-identical to the
recorded `r1.patch`, unit finishes within tolerance of its recorded service time.

| Backend | Units ok | Manifest wall time | Per-unit makespan (u0000) | Total slide | Patches identical |
| ------- | -------- | ------------------ | ------------------------- | ----------- | ----------------- |
| podman  | 5/5 | 224s | 49.1s (recorded 49.2s) | 1.9s | 5/5 |
| vm (qemu, KVM) | 5/5 | 206s | 49.8s | 2.6s | 5/5 |
| fc (perexec) | 5/5 | 204s | 50.2s | 3.0s | 5/5 |

No spec prediction — this is a correctness bring-up, not a performance measurement. The
three backends landing within ~1s of each other and of the recording is a sanity signal
only (see §5 on timing contamination).

## 3. Deltas from the design

1. **The fc backend's guest kernel is Firecracker's CI kernel (6.18.56), not the host's.**
   The host-kernel rule (kernel = build host = experiment host) is maintained for the qemu
   backend, but cannot hold for firecracker: Ubuntu generic kernels build vsock as a module
   (`CONFIG_VIRTIO_VSOCKETS=m`), firecracker guests boot without an initrd, and moca's
   pinned guest init does not modprobe — the agent dies with `vsock socket: address family
   not supported by protocol`. Using the moca-documented FC CI kernel is the
   moca-compatible path; accepted as-is. Consequence: fc-vs-qemu comparisons on this host
   are a VMM-PLUS-KERNEL pair (same caveat the side-by-side report already records for its
   6.8.0-31 vs 6.8.0-147 pair).
2. **Images are built from Dockerfiles, not pulled from `icr.io/vopo`.** The registry has
   garbage-collected at least `swebench-django__django-11133` (anonymous and authenticated
   pulls both return `manifest unknown`, even by the digest of a local copy pulled 6 days
   earlier). First-principles builds were already the plan; the registry is no longer a
   fallback for at least that instance.
3. **The qemu backend's initrd is a slim dracut image, not `/boot/initrd.img-*`.** Required
   by the dracut hostonly default on 26.04 (§4.1); the old behavior (copy the host's initrd)
   survives only on initramfs-tools hosts. Script change, `cc15754`.

## 4. Findings

### 4.1 dracut hostonly initrd hangs the guest boot

- **Symptom:** `vm-snapshot` timed out at "did not reach a shell within 180s" — looked
  like a KVM or kernel problem; the console showed `dracut-initqueue` waiting on
  `dev-disk-by\x2duuid-75…`, a UUID that is the *build host's* root disk.
- **Root cause:** Ubuntu 26.04 ships dracut; its hostonly `/boot/initrd.img-*` embeds the
  host's root-disk UUID and refuses `root=/dev/vda`. (A stock no-hostonly rebuild then
  panicked at 1 GiB — 108 MB initrd, "System is deadlocked on memory" at unpack.)
- **Fix:** `prepare_qcow2_images.sh` builds a slim no-hostonly initrd on dracut hosts
  (`base rootfs-block fs-lib shutdown` + virtio/ext4 drivers, 36 MB; boots to shell in ~2s
  at `VM_MEM_MB=1024`). `INITRD_IMG` overrides. Commit `cc15754`.

### 4.2 qemu 10.x "info migrate" wording broke the snapshot wait

- **Symptom:** `state save did not complete within 600s` after a snapshot run — and the
  "failed" run had already written a complete, valid `state.bin` (222,832,358 bytes, both
  failures byte-identical).
- **Root cause:** `vm_snapshot` polled for the literal string `Migration status:
  completed`; qemu 10.x prints `Status:\t\tcompleted`. The migration itself finished in
  1.6s (218 MiB transferred, 211k zero pages skipped — hence a ~222 MB state file for a
  1 GiB guest being *complete*, not partial).
- **Fix:** `replay.py` matches the status word across qemu wordings and surfaces
  failed/cancelled/error explicitly. Commit `29f849e`.

### 4.3 state.bin left root-owned after a sudo build

- **Symptom:** every `vm-start` as the unprivileged user would have failed with
  permission denied reading `state.bin` (0640 root:root).
- **Root cause:** the `vm/` chown in `prepare_qcow2_images.sh` ran *before* the snapshot
  step, but `state.bin` is written by root-owned qemu during it.
- **Fix:** chown `state.bin` after the snapshot. Commit `b2983fa`.

### 4.4 Golden-snapshot preflight requires swap off

- **Symptom:** `build-snapshot.sh` refused: "swap must be off on a snapshot-build host".
- **Root cause:** correct by design — a swapped-out guest page would make the memory
  digest and restore behavior disagree. The host had an unused 8 GiB `/swap.img`.
- **Fix:** `swapoff -a` at bring-up. **Not persisted**: after a reboot, swap comes back and
  must be disabled again before snapshot builds (see §5).

## 5. What is NOT verified

- **No load runs.** One smoke manifest per backend, peak concurrency ~2. Nothing here
  speaks to density, stretch under load, or the moca/fc machinery costs — the side-by-side
  report's numbers were measured on the old 32-core hosts and do not transfer.
- **Smoke timings are contaminated.** The podman smoke ran concurrently with container
  image builds and the first qcow2 build (deliberately, per user call); the vm smoke
  overlapped unrelated host work early on. The 49.1/49.8/50.2s makespans are correctness
  signals, not measurements to cite.
- **Golden snapshots not yet exercised by moca itself.** They are built by moca's own
  pipeline and are the artifact a moca install on this host would consume
  (`MICROVM_SNAPSHOT_DIR=/srv/sbx/snapshots`), but no moca control plane has run on this
  host — `--backend moca` is untested here.
- **Swap-off survives reboot: no.** `/swap.img` is still in fstab; snapshot builds after a
  reboot need `swapoff -a` first (or an fstab edit, deliberately not made).
- **Snapshot host-pinning is asserted, not tested.** The manifests record the instance
  type (AS -4124GS-TNR); cross-host restore was never attempted and is not expected to
  work.
- **One smoke instance only.** All 5 units are `django__django-11133` r1 — the manifest's
  own scope. `django__django-10914` and `pydata__xarray-4094` have built artifacts and
  verified snapshots but zero replayed steps on this host.
- **fc `warm` mode (`FC_VM_MODE=warm`) not run** — only the default perexec lifecycle.

## 6. Follow-ups

| What | Why it was deferred | Issue |
| ---- | ------------------- | ----- |
| Disable swap in fstab (or a boot-time swapoff) | Host-level config change; left for an explicit decision | — |
| moca backend bring-up on this host | Needs the moca control-plane install; not part of this bring-up | — |
| Re-run smokes cleanly (no concurrent builds) once new trajectories land | Timings from 2026-11-04 are contaminated; the incoming trajectory set supersedes them anyway | — |
| Update smoke manifest(s) for the new trajectory set | Merge in flight; smoke should validate what will actually be measured | — |

---

*Assisted-By: Claude Code*
