# replay.py — usage

Replays a recorded container-hands trajectory (`.trajectory.json` from a run)
against a sandbox, step by step, and reports whether every recorded tool step
succeeds again. The sandbox is either a podman container or a VM; the
trajectory format and the report are identical for both.

## The drivers

- **`replay_trajectory.py <runs_dir> [--backend podman|vm] [--reps r1]
  [--timed] [--keep] [--dry-run]`** — replays the configured instances'
  trajectories, one sandbox per run, with the PASS criteria (all steps ok,
  patch identical, testbed pristine). Replaces `replay_r1.sh` (containers)
  and `replay_vm.sh` (VMs), which are kept only as historical reference.
- **`replay_manifest.py <manifest.json> [--backend podman|vm] [--dry-run]
  [--keep]`** — same sandboxes, same flags, but executes a load manifest:
  one sandbox per unit, started at its appointed `start_s`, timed replay.
- Both drivers pick the backend with the same `--backend` flag and go
  through `replay.py`'s `Sandbox` classes (`PodmanSandbox`, `VmSandbox`);
  neither knows how a sandbox is started or stopped.

## Quick start

The drivers (what you normally want):

```sh
python3 -I replay_trajectory.py ../runs/container                  # podman
python3 -I replay_trajectory.py ../runs/container --backend vm     # VMs
python3 -I replay_manifest.py manifests/smoke.json --backend vm --dry-run
```

By hand (what the drivers do for you):

```sh
podman run -d --name c --user 1000:1000 -w /testbed \
    icr.io/vopo/swebench-django__django-10914:latest sleep infinity
python3 -I replay.py ../runs/container/am5-l30-a.django__django-10914.r1.trajectory.json c
python3 -I replay.py git-diff c > replayed.patch   # diff after the replay
podman rm -f c
```

```sh
sock=/tmp/myvm.sock
python3 -I replay.py vm-start ../images/django__django-10914/vm $sock
python3 -I replay.py ...trajectory.json vm:$sock
python3 -I replay.py git-diff vm:$sock > replayed.patch
python3 -I replay.py vm-stop $sock
```

Or let one invocation manage the whole VM lifetime (one operation only — the
VM is destroyed afterwards, so a `git-diff` would see the *pre*-replay tree):

```sh
python3 -I replay.py ...trajectory.json qemu:../images/django__django-10914/vm
```

## Targets

| target           | meaning                                                       |
|------------------|---------------------------------------------------------------|
| `<container>`    | a running podman container (bare name, original CLI form)     |
| `podman:<name>`  | the same, explicit                                            |
| `vm:<socket>`    | a VM booted by `vm-start`; persists until `vm-stop`           |
| `qemu:<vm-dir>`  | ephemeral VM: boot, run this one operation, shut down         |

`vm-dir` is the `vm/` directory produced by `images/to_qcow2.sh`; it must
contain exactly one `.qcow2`, one `vmlinuz-*`, and one `initrd*`.

## Modes

```
replay.py TRAJECTORY.json TARGET [TIMELINE.txt]   replay a trajectory
replay.py vm-start VM_DIR SOCKET                  boot a VM, wait for its shell
replay.py vm-stop SOCKET                          shut a booted VM down
replay.py git-status TARGET                       print `git status --porcelain` in /testbed
replay.py git-diff TARGET                         print `git diff HEAD --binary` on stdout
replay.py                                         print the short usage
```

With a `TIMELINE.txt` the replay is timed: each step fires at its recorded
time, and a step that overruns its recorded duration slides every later step
by the overrun. `TIMED=1 replay_vm.sh` (or `replay_r1.sh`) enables this.

Steps run as uid 1000, workdir `/testbed`, with the testbed conda env first
on PATH — the environment the agent's tools ran in. Exit status is 0 iff
every recorded tool step succeeded again; one line is printed per step
(`ok` / `FAIL` / `MISMATCH` against the recorded `is_error`).

## How the VM backend works

- **Start**: if `vm-dir` has a `state.bin` (saved by `vm-snapshot`, normally
  the last step of `images/to_qcow2.sh`), `vm-start` resumes the VM from it
  in under a second — incoming migration over a throwaway `snapshot=on`
  overlay, so the base qcow2 stays pristine and nothing survives the run
  (container-like cleanup). Without `state.bin`, it boots the long way
  (direct kernel boot, root on `/dev/vda`, `init=/bin/bash` on
  `console=ttyS0`) and runs the setup itself.
- **Prep (`vm-snapshot`)**: boots once without an overlay (the only path
  that writes the base image), runs the standard setup — mounts `/dev/shm`
  and `/run` (what a container runtime provides implicitly), silences
  kernel printks and console echo — and saves full VM state via migration.
  Must run under the same accelerator as the loads (KVM in production);
  snapshots are accelerator-pinned.
- **Exec**: scripts travel base64-encoded to the guest (recorded commands
  can contain anything, including heredocs), are written to
  `/tmp/.replay-step.sh`, and run as uid 1000 via `setpriv`. Output is
  framed between random begin/end markers so kernel or prompt noise never
  leaks into step output. The Read/Edit tool is inlined the same way — no
  files are ever shipped into the guest.
- **Requires in the guest**: `setpriv`, `base64`, `bash`, `python` — all
  present in the Ubuntu-based swebench images.
- **`git-diff`** base64-encodes in the guest so binary hunks survive the
  console byte stream, and decodes before writing to stdout.

## Extending to other sandboxes

A backend is anything with a `run(script, timeout=1800)` method returning an
object with `.returncode`, `.stdout`, `.stderr` (see `PodmanTarget` and
`VmTarget`; the whole transport — podman exec, serial console, a future
remote microVM API — lives inside `run`). Add the class, add a prefix in
`make_target`, and every mode above works against it.
