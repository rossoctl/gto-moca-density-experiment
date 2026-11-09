# Host preflight runbook — conditioning a density-experiment host

Date: 2026-11-08 (experience from srv-r18b14s27 bring-up, C100–C400 campaigns)
Audience: the techs setting up quasi-production hosts for this experiment
stack. Written for humans; every command is copy-pasteable. Where a value
looks arbitrary, the "why" names the failure we hit without it.

Reference host: srv-r18b14s27 — 192 cores, 499 GiB RAM, Ubuntu 26.04.1 LTS.
Everything below was learned by running the C100/C200/C400 pressure ladders;
the limits are real, not theoretical. See also
[2026-11-08-rootless-podman-density-limits.md](2026-11-08-rootless-podman-density-limits.md)
for the podman failure analysis.

---

## 1. OS baseline

Ubuntu 24.04+ (we run 26.04). Install:

```sh
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get install -y emacs qemu-system-x86 qemu-utils podman golang-go \
                        linux-headers-$(uname -r)   # extract-vmlinux lives here
```

Firecracker + jailer: not in Ubuntu repos. Install the official release
binaries to `/usr/local/bin` (verify the tarball sha256 against the
release's `.sha256.txt`):

```sh
curl -sL -o fc.tgz https://github.com/firecracker-microvm/firecracker/releases/download/v1.17.0/firecracker-v1.17.0-x86_64.tgz
# ... verify, extract, then:
sudo install -m 755 firecracker-v1.17.0-x86_64 /usr/local/bin/firecracker
sudo install -m 755 jailer-v1.17.0-x86_64 /usr/local/bin/jailer
```

## 2. The user account

The experiment user (e.g. `galmasi`) needs:

```sh
sudo usermod -aG kvm <user>        # /dev/kvm access; takes effect on NEXT login
```

## 3. Swap OFF

Firecracker golden-snapshot builds refuse to run with swap on (a swapped-out
guest page would make the memory digest and restore behavior disagree):

```sh
sudo swapoff -a                    # now
sudo sed -i.bak '/swap/s/^/#/' /etc/fstab   # and after reboot
```

We left fstab unedited on the reference host; **do edit it** on new hosts —
`build-snapshot.sh` fails otherwise after every reboot.

## 4. Kernel sysctls — the invisible density limits

These three have no effect at low density and then kill a run with
misleading error text. Set them on every host:

### 4.1 Keyring quota (rootless containers)

```sh
echo kernel/keys/maxkeys=20000 | sudo tee /etc/sysctl.d/99-density.conf
echo kernel/keys/maxbytes=20000000 | sudo tee -a /etc/sysctl.d/99-density.conf
sudo sysctl --system
```

- **Why:** crun mints a session keyring per container; the per-user quota
  defaults to **200**. Container #201 dies with `crun: create keyring
  <hash>: Disk quota exceeded` — nothing is wrong with the disk.
- **Without it:** rootless podman caps at ~200 concurrent containers.

### 4.2 File descriptors

```sh
printf "[Service]\nLimitNOFILE=65536\n" | sudo tee /etc/systemd/system/user@1000.service.d/limits.conf
sudo systemctl daemon-reload
```

AND add to `/etc/security/limits.conf` (sshd sessions read this, not the
systemd drop-in):

```
<user>  soft  nofile  65536
<user>  hard  nofile  65536
```

- **Why:** systemd/sshd default NOFILE is 1024. The workload driver plus
  per-step subprocesses hold several fds per live sandbox; ~330 live hits
  `[Errno 24] Too many open files`.
- **ALSO:** every run command should still start with `ulimit -n 65536` —
  belt and suspenders; the limits.conf path depends on PAM.
- **AND the driver must not use `select()`:** raising NOFILE past 1024
  surfaces the next ceiling — `select()`'s fd_set is capped at FD_SETSIZE
  (1024) no matter the ulimit. At C800 10pct churn the vm driver's console
  sockets crossed fd 1024 and 6,174 units failed instantly with
  `filedescriptor out of range in select()`. Fixed 2026-11-09: `Console`
  in `replay.py` uses `select.poll()` (no fd-number limit). Keep `replay.py`
  at or past that fix on every host.

### 4.3 Locked memory (qemu / io_uring) — the subtle one

Add to `/etc/security/limits.conf` (per-user, picked up by new logins):

```
<user>  soft  memlock  unlimited
<user>  hard  memlock  unlimited
```

- **Why:** qemu 10.x defaults its block backend to io_uring, and io_uring
  ring memory is charged against the user's RLIMIT_MEMLOCK **aggregated
  across every process of that UID** (default 8 MB). The ~230rd concurrent
  qemu works; the ~319th dies with `Failed to initialize io_uring: Cannot
  allocate memory` — with hundreds of GB of RAM free. Nothing about the
  individual process is wrong; the per-UID account is exhausted.
- **Verify from a NEW login** (`ulimit -l` must print `unlimited`) — old
  sessions keep the old limit.

## 5. Storage layout

All on one filesystem (hardlinks):

```
/srv/sbx/vmlinux-fcci-<ver>     # Firecracker CI guest kernel (see §7)
/srv/sbx/rootfs/<template>/     # moca rootfs trees
/srv/sbx/snapshots/<template>/  # golden snapshots (host-pinned: do NOT copy
                                # between hosts; rebuild on each)
/srv/sbx-purefc/                # fc backend jails/workspaces -- SAME fs as
                                # /srv/sbx (hardlinks), never under /srv/sbx
```

The qcow2 VM images and workspaces live under the experiment repo checkout
(`images/<instance>/vm/`). Disk pressure at C400-scale: hundreds of GB
transient; plan ≥ 2 TB free.

## 6. Building the guest assets (per host — they are host-pinned)

From the experiment repo (`gto-moca-density-experiment`), after cloning the
pinned moca repo (public) to `~/repo/rossoctl/moca`:

```sh
cd images
./prepare_ctr_images.sh                                   # container testbeds
sg kvm -c "sudo ./prepare_qcow2_images.sh"                # SWE-bench VMs
sg kvm -c "sudo ./prepare_tb_qcow2.sh"                    # Terminal-Bench VMs
MOCADIR=~/repo/rossoctl/moca ./prepare_moca_images.sh     # moca template images
# then per TB template, the moca pipeline (root podman storage):
#   build-rootfs.sh / build-snapshot.sh  -- see the bring-up report for the
#   exact invocations and the tb-worker-overlay step
MOCADIR=~/repo/rossoctl/moca ./prepare_fc_backend.sh      # fc guest client
```

Two hard-won portability notes baked into those scripts:

- **Host kernel only for qemu VMs** (kernel = build host = experiment host,
  eliminating a variability; `vm-snapshot` state is accelerator-pinned).
- **Ubuntu ≥ 26.04 ships dracut hostonly initrds** that hang a
  `root=/dev/vda` guest in `dracut-initqueue` forever. The scripts detect
  dracut and build a slim generic initrd (base + rootfs-block + virtio/ext4,
  ~36 MB). If a VM build times out at "did not reach a shell", this is why.

## 7. The firecracker guest kernel

Do NOT use the host kernel for fc guests (Ubuntu's vsock is modular; fc
guests boot without an initrd and the moca init does not modprobe — the
guest agent dies with `vsock socket: address family not supported`). Use
Firecracker's CI kernel per moca's docs:

```sh
S3="https://s3.amazonaws.com/spec.ccfc.min"
CI=$(curl -fsSL "$S3?list-type=2&prefix=firecracker-ci/&delimiter=/" \
  | grep -oP '(?<=<Prefix>)firecracker-ci/[0-9]{8}-[^/]+/(?=</Prefix>)' | sort | tail -1)
KEY=$(curl -fsSL "$S3?list-type=2&prefix=${CI}x86_64/vmlinux-" \
  | grep -oP "(?<=<Key>)${CI}x86_64/vmlinux-[0-9.]+(?=</Key>)" | sort -V | tail -1)
curl -fsSL -o /srv/sbx/vmlinux-fcci "https://s3.amazonaws.com/spec.ccfc.min/$KEY"
```

## 8. Preflight verification (run ALL of these before declaring a host ready)

```sh
# limits took effect (from a NEW login):
ulimit -n            # 65536
ulimit -l            # unlimited
cat /proc/sys/kernel/keys/maxkeys   # 20000

# swap off:
free -h | grep -i swap              # 0B

# KVM usable as the user:
sg kvm -c "kvm-ok" || ls -l /dev/kvm   # crw-rw---- root kvm

# one VM boots and resumes (sub-second) from its snapshot:
cd <repo>/replay
sg kvm -c "python3 -I replay.py vm-start ../images/tb30/fix-git/vm /tmp/pf.sock"
python3 -I replay.py vm-stop /tmp/pf.sock

# each backend passes the smoke manifest:
python3 -I replay_manifest.py manifests/smoke.json --backend podman   # 6/6
sg kvm -c "python3 -I replay_manifest.py manifests/smoke.json --backend vm"
sudo env FC_VM_MODE=warm bash -c "cd <repo>/replay && python3 -I replay_manifest.py manifests/smoke.json --backend fc"
```

Expected: 6/6 units ok on each. (On fc, regex-log reports 1 known
environment-divergence mismatch; that is documented and expected.)

## 9. Failure-symptom quick reference

| Symptom | Actual cause | Fix |
| --- | --- | --- |
| `crun: create keyring: Disk quota exceeded` | per-user keyring quota (200) | §4.1 |
| `[Errno 24] Too many open files` in the driver | NOFILE 1024 | §4.2 |
| `filedescriptor out of range in select()` at sandbox start, high density | driver console code used `select()`; fds crossed FD_SETSIZE (1024) once NOFILE was raised | §4.2 — fixed in `replay.py` (poll), 2026-11-09 |
| `Failed to initialize io_uring: Cannot allocate memory` (qemu, RAM free) | per-UID MEMLOCK (8 MB) exhausted by io_uring rings | §4.3 |
| `database is locked` (podman, persistent) | podman sqlite store saturated — architectural, not a sysctl | see the podman note; daemon-based runtime or lower op rate |
| VM build hangs: `did not reach a shell within 180s` | dracut hostonly initrd (Ubuntu ≥ 26.04) | scripts auto-build slim initrd; check `INITRD_IMG` |
| `state save did not complete` (false timeout, state.bin complete) | qemu 10.x `info migrate` wording | already fixed in `replay.py` |
| Snapshot build refuses: swap must be off | swap on | §3 |
| fc guest agent: `vsock: address family not supported` | host kernel in fc guest | §7 |

## 10. Operational cautions learned the hard way

- **Never kill a measurement run midway** without also killing the
  per-unit step engines (`pkill -f 'replay.py'`) — they survive the driver,
  re-attach to same-named containers, and contaminate the next run.
- The resource sampler must be stopped with **SIGTERM** (`timeout -s TERM`);
  background jobs ignore SIGINT (POSIX), and `timeout -s INT` silently
  no-ops.
- Run manifests from a fresh login shell after any limits change; verify
  with `ulimit -a` first.
