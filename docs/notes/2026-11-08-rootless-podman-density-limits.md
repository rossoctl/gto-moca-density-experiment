# Rootless podman hits three density walls below C200 — none of them hardware

Date: 2026-11-08
Context: the load200 mixed-duty rungs on srv-r18b14s27 (192 cores, 499 GiB,
Ubuntu 26.04, podman 5.7.0 rootless). Full logs in `replay/logs/load200/`
(attempts 1–3 of `2pct-podman`) and the C100/C200 campaign reports.

## The short version

Rootless podman could not sustain ~200 concurrent containers with ~2.6
creates/s, ~2.6 removes/s and ~29 execs/s — on a machine that sat at **3%
CPU** throughout the collapse. Three separate limits fell over in sequence;
two are kernel/systemd defaults (fixable), the third is architectural
(podman's per-user sqlite store). The QEMU and firecracker backends ran the
same manifests at the same pressure with zero stretch, because their step
transport is a socket, not a per-step CLI.

If you are driving rootless podman at hundreds of concurrent containers or
tens of CLI operations per second, expect all three.

## Wall 1: kernel keyring quota (200 per user)

- **Symptom:** container start fails with the misleading
  `crun: create keyring <hash>: Disk quota exceeded` — nothing is wrong with
  disk.
- **Mechanism:** crun mints a session keyring per container; the per-user
  key quota (`/proc/sys/kernel/keys/maxkeys`) defaults to **200**. The 201st
  concurrent container dies. We first saw it at ~170 live (C200 attempt 1:
  4,163 of 4,690 units failed at start).
- **Fix:** `sudo sysctl kernel/keys/maxkeys=20000 kernel/keys/maxbytes=20000000`
  (persisted in `/etc/sysctl.d/99-keyring-quota.conf`). C100's peak of 129
  had lived under the quota — this wall is invisible until you cross it.

## Wall 2: file descriptors (1024 per user session)

- **Symptom:** the workload driver fails with `[Errno 24] Too many open files`
  (C200 attempt 2: 3,301 units).
- **Mechanism:** systemd user sessions default `LimitNOFILE=1024`. A driver
  holding per-unit logs plus per-step `podman exec` subprocess pipes needs
  several fds per live unit; ~330 live containers exhausted it.
- **Fix:** `ulimit -n 65536` for the driver, plus
  `/etc/systemd/system/user@1000.service.d/nofile.conf` with
  `LimitNOFILE=65536` to persist it for the session.

## Wall 3: podman's sqlite store (architectural — not configurable away)

- **Symptom:** container start fails with `Error: beginning transaction:
  database is locked` (C200 attempt 3: 3,495 units) — **persistently**, not
  transiently: 5 retries with backoff could not acquire the lock.
- **Mechanism:** every podman CLI invocation — `run`, `rm`, `exec`, even
  `ps` — opens the per-user store database
  (`~/.local/share/containers/storage/db.sql`), a single-writer sqlite file
  with a write transaction (fsync) per state change. At ~5 write
  transactions/s plus ~29 execs/s of reader traffic, the writer queue never
  drains. C100 (~14 execs/s) held; C200 (~29/s) collapsed. CPU idled at 3%
  the entire time.
- **Not a fix:** retries (the lock is saturation, not contention spikes);
  a separate store per run (the concurrency is within one run); busier
  timeouts (the queue is deeper than any timeout).
- **What would fix it:** bypass the CLI-per-operation pattern — raw crun,
  or podman's API socket (`podman system service`) with a persistent
  client. We did not pursue this: containers are consistency checks in
  this experiment, not the subject.

## Why the VM backends did not hit any of this

The QEMU backend's steps travel over a per-VM unix console socket; the
firecracker backends' over vsock. No per-step CLI process, no shared
database, no keyrings. The same host ran the same manifests at C200 with
4,693/4,693 units and −2.9% median stretch (QEMU), i.e. the machine was
never the constraint — the container *tooling* was.

## Docker, for completeness

Docker would likely clear all three: dockerd is a persistent daemon (no
per-op sqlite open — the CLI is a thin socket client), it runs rootful
(root's keyring quota is a different, much larger bucket), and its systemd
unit ships `LimitNOFILE=infinity`. The cost is a privileged daemon on the
host. Rootless docker would re-inherit walls 1 and 2.

## Reproduction sketch

A driver that creates N concurrent `sleep infinity` containers as a
rootless user, execs a trivial command in each every few seconds, and
removes them, will hit wall 1 at ~200 concurrent (default sysctls), wall 2
around ~300 live (default NOFILE, driver-shaped), and wall 3 at roughly
~15–25 CLI ops/s sustained regardless of the other two.
