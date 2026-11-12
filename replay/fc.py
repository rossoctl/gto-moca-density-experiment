#!/usr/bin/env python3
"""replay/fc.py — a PURE firecracker backend for the replay drivers.

The argument this backend exists to settle: does moca's stop-start machinery
(relay, control plane, workers, standby pool, token path, per-exec reap)
buy anything over raw firecracker doing the same thing?  The memory wins of
snapshot-restored microVMs are known to come from firecracker itself (every
VM restored from a golden snapshot shares that snapshot's physical pages —
see docs/reports/moca-side-by-side-report.md); this backend removes the moca
stack from the path so the remaining difference is measurable.

It reuses moca's golden snapshots VERBATIM — /srv/sbx/snapshots/<template>/
(kernel, rootfs, memfile, vmstate, agent) — and copies the production
launcher's restore recipe (remote-worker internal/vmpool/launcher_firecracker
.go): a jail directory of hardlinks at fixed jail-relative names, jailer
chroot, PUT /snapshot/load with a vsock_override, exec over the guest agent,
process-group kill.  Fresh VM per exec, persistent /workspace per sandbox:
the same lifecycle model as the moca backend, so a manifest run on
--backend moca vs --backend fc differs only by the management stack.

Requirements (prepare once per host with images/prepare_fc_backend.sh):
  - /usr/local/bin/firecracker and /usr/local/bin/jailer (the moca install)
  - the golden snapshots the moca workers use
  - the built guest client (speaks the guest agent's framed protocol over
    the vsock uds; generated from the pinned moca checkout's internal/
    guestagent so the wire format has exactly one implementation)
  - ROOT: jailer chroots and joins cgroups, so the driver must run under
    sudo.  The moca backend needs no root; this one does.

Coexistence with a moca stack on the same machine:
  - everything this backend creates lives under FC_BASE (default
    /srv/sbx-purefc: jail/ and workspaces/), never under /srv/sbx;
    hardlinks require FC_BASE on the same filesystem as the snapshots.
  - moca's worker memory budget does not know about these VMs: cap this
    backend with FC_MAX_VMS (default 36, mirroring a 39.8-GiB budget of
    1-GiB sweb VMs) and leave moca's limits alone; for load runs, use one
    stack at a time or split the budget deliberately.
  - no ports are used (per-jail unix sockets), /dev/kvm is shared.
"""

import base64
import fcntl
import os
import re
import shutil
import subprocess
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))

# --- configuration (env-overridable) ------------------------------------------

FC_BASE = os.environ.get("FC_BASE", "/srv/sbx-purefc")
FC_SNAPSHOTS = os.environ.get("FC_SNAPSHOTS", "/srv/sbx/snapshots")
FC_FIRECRACKER = os.environ.get("FC_FIRECRACKER", "/usr/local/bin/firecracker")
FC_JAILER = os.environ.get("FC_JAILER", "/usr/local/bin/jailer")
FC_GUEST_CLIENT = os.environ.get(
    "FC_GUEST_CLIENT", os.path.join(HERE, "fc-guest-client"))
FC_UID = int(os.environ.get("FC_UID", "1000"))
FC_GID = int(os.environ.get("FC_GID", "1000"))
FC_MAX_VMS = int(os.environ.get("FC_MAX_VMS", "36"))
# Lifecycle: "perexec" (default) mirrors the moca backend exactly -- a fresh
# VM restored from the golden snapshot for every step, killed after -- which
# isolates moca's management-stack cost.  "warm" keeps each sandbox's VM
# resident across steps (no on/off stutter: the guest page cache stays warm,
# the cold-rootfs cost vanishes), which is the firecracker analogue of the
# VM backend's long-lived QEMU processes.  Both rows belong in the report.
FC_VM_MODE = os.environ.get("FC_VM_MODE", "perexec")
FC_VSOCK_PORT = 1024
WORKSPACE_IMAGE_BYTES = 2 * 1024 * 1024 * 1024   # matches the moca launcher

# the snapshot files the jail hardlinks (launcher_firecracker.go's names)
_SNAPSHOT_FILES = ("vmstate", "memfile", "kernel", "rootfs", "agent")

# the replay wrapper's PATH inside sweb images (same as the moca backend)
_TESTBED_ENV_PATH = ("/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:"
                     "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


def fc_template(instance):
    """Instance id -> golden snapshot dir name (same mapping as the moca
    backend's moca_template). SWE-bench ids carry the repo as a '__'-joined
    prefix (django__django-11133 -> sweb-django-11133); Terminal-Bench task
    ids are bare (fix-git -> tb-fix-git)."""
    if "__" in instance:
        return "sweb-" + instance.split("__", 1)[1]
    return "tb-" + instance


class FcError(RuntimeError):
    pass


# --- one restored VM -----------------------------------------------------------

class FcVM:
    """One firecracker VM restored from a golden snapshot, mirroring the
    production launcher's Restore(): jail of hardlinks, jailer, snapshot/load
    with vsock_override, exec over the guest agent, process-group kill.

    Fresh VM per exec is the POINT (the moca lifecycle model); the /workspace
    drive is the sandbox's persistent image, hardlinked into each jail."""

    def __init__(self, snapshot_dir, workspace_img):
        self.snapshot_dir = snapshot_dir
        self.workspace_img = workspace_img
        self.id = "fc-" + uuid.uuid4().hex[:12]
        # jailer v1.17's layout: <chroot-base>/<vmm-name>/<id>/root
        # (verified on hardware; the launcher source's composition was
        # misread as <base>/<id>/<id> on the first run)
        self.jail = os.path.join(FC_BASE, "jail", "firecracker", self.id, "root")
        self.jail_parent = os.path.dirname(self.jail)
        self.proc = None
        # console log OUTSIDE the jail tree: stop() removes the jail, and the
        # log is the only artifact that explains a VM that never came up
        os.makedirs(os.path.join(FC_BASE, "logs"), exist_ok=True)
        self.console_log = os.path.join(FC_BASE, "logs", self.id + ".log")

    def start(self, api_wait_s=10.0, vsock_wait_s=15.0, resume=True,
              mount_workspace=True):
        try:
            self._start(api_wait_s, vsock_wait_s, resume, mount_workspace)
        except Exception:
            # never leak a running firecracker: the first hardware run
            # timed out waiting for the API socket (wrong jail path) and
            # left five VMs running with no owner
            self.stop()
            raise

    def _start(self, api_wait_s, vsock_wait_s, resume=True,
               mount_workspace=True):
        os.makedirs(self.jail, exist_ok=True)
        os.makedirs(os.path.join(self.jail, "run"), exist_ok=True)
        for name in _SNAPSHOT_FILES:
            dst = os.path.join(self.jail, name)
            os.link(os.path.join(self.snapshot_dir, name), dst)
            os.chown(dst, FC_UID, FC_GID)
        os.link(self.workspace_img, os.path.join(self.jail, "workspace.img"))
        os.chown(os.path.join(self.jail, "workspace.img"), FC_UID, FC_GID)

        log = open(self.console_log, "wb")
        self.proc = subprocess.Popen(
            [FC_JAILER, "--id", self.id,
             "--exec-file", FC_FIRECRACKER,
             "--uid", str(FC_UID), "--gid", str(FC_GID),
             "--chroot-base-dir", os.path.join(FC_BASE, "jail"),
             "--", "--api-sock", "/run/firecracker.socket"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # the jailer execve's into firecracker, so the pid is the VM's
        api_sock = os.path.join(self.jail, "run", "firecracker.socket")
        self._wait_file(api_sock, api_wait_s, "firecracker API socket")
        # resume=False leaves the VM paused after snapshot load (idle-tax
        # microbench: no vCPU thread runs, no guest agent answers); with it,
        # mount_workspace=False skips the /workspace mount (the bench's VMs
        # share one unmounted workspace image, so there is nothing to mount
        # and concurrent rw mounts would corrupt it anyway).
        body = """{
            "snapshot_path": "/vmstate",
            "mem_backend": {"backend_path": "/memfile", "backend_type": "File"},
            "vsock_override": {"uds_path": "/run/v.sock"},
            "resume_vm": %s
        }""" % ("true" if resume else "false")
        self._api_put(api_sock, "/snapshot/load", body)
        if not resume:
            return
        self._wait_file(os.path.join(self.jail, "run", "v.sock"),
                        vsock_wait_s, "vsock socket")
        if not mount_workspace:
            return
        # the guest does NOT mount /workspace itself: the production
        # launcher's Resume runs this exact command over vsock right after
        # restore (launcher_firecracker.go).  Without it /workspace is a
        # placeholder dir on the read-only rootfs and every write fails
        # with EROFS -- the first hardware run's seed failure.
        rc, out, err = self.exec(
            "mountpoint -q /workspace || "
            "mount -o rw,noatime /dev/vdb /workspace", 30)
        if rc != 0:
            raise FcError(f"mount /workspace failed rc={rc}: "
                          f"{(err or out).strip()[-300:]}")

    def exec(self, command, timeout_s):
        """Run a command in the guest via the agent. Returns (rc, stdout,
        stderr) with mocactl-like rc mapping: the command's own code, or 125
        for machinery failures (agent/protocol), 124 for a client-side
        timeout."""
        try:
            p = subprocess.run(
                [FC_GUEST_CLIENT, "-uds", os.path.join(self.jail, "run", "v.sock"),
                 "-port", str(FC_VSOCK_PORT), "-timeout-s", str(int(timeout_s)),
                 "-command", command],
                capture_output=True, text=True, timeout=timeout_s + 120)
        except subprocess.TimeoutExpired:
            return 124, "", "fc: guest client timed out"
        if p.returncode == 1 and "guest_client:" in (p.stderr or ""):
            # guest_client exits 1 for ITS OWN dial/protocol errors; the
            # command's exit code arrives as guest_client's exit code
            return 125, p.stdout, p.stderr
        return p.returncode, p.stdout, p.stderr

    def stop(self, term_grace_s=5.0):
        # SYNC BEFORE THE KILL, or the VM's writes die in its page cache:
        # the production launcher's wrapCommand exists for exactly this
        # ("sync before the VM dies"), and the first hardware run lost the
        # seed's cp this way -- the next VM mounted an empty workspace.
        # Best-effort and bounded: a VM that cannot answer is killed anyway.
        if self.proc and self.proc.poll() is None:
            try:
                self.exec("sync", 15)
            except Exception:
                pass
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
                for _ in range(int(term_grace_s * 10)):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.1)
                else:
                    os.killpg(self.proc.pid, 9)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.jail_parent, ignore_errors=True)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # -- internals ------------------------------------------------------------

    def _wait_file(self, path, seconds, what):
        deadline = time.monotonic() + seconds
        while not os.path.exists(path):
            if time.monotonic() > deadline:
                raise FcError(
                    f"{what} never appeared ({path}); console log: "
                    f"{self._console_tail()}")
            if self.proc and self.proc.poll() is not None:
                raise FcError(
                    f"jailer/firecracker exited rc={self.proc.returncode} "
                    f"waiting for {what}; console log: {self._console_tail()}")
            time.sleep(0.05)

    def _console_tail(self):
        try:
            with open(self.console_log, "rb") as f:
                return f.read()[-2000:].decode("latin-1")
        except OSError:
            return "<no console log>"

    def _api_put(self, unix_sock, path, body, timeout_s=30.0):
        """One HTTP PUT over the jail's API socket. Firecracker's API is
        plain HTTP/1.1 on a unix socket. The response is framed by
        Content-Length (firecracker's micro-http does NOT honor
        Connection: close -- reading to EOF times out, which is exactly
        what the first hardware run hit), so read headers, then exactly
        Content-Length bytes, then close."""
        import socket
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        try:
            s.connect(unix_sock)
            req = (f"PUT {path} HTTP/1.1\r\nHost: localhost\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n"
                   f"{body}")
            s.sendall(req.encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = s.recv(65536)
                if not chunk:
                    raise FcError(f"firecracker API {path}: EOF before headers")
                resp += chunk
            head, _, rest = resp.partition(b"\r\n\r\n")
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":", 1)[1].strip())
            while len(rest) < clen:
                chunk = s.recv(65536)
                if not chunk:
                    break
                rest += chunk
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if " 200" not in status and " 204" not in status:
                raise FcError(f"firecracker API {path}: {status}: "
                              f"{(rest or resp)[-500:].decode('latin-1')}")
        finally:
            s.close()


# --- the replay Sandbox/Target pair --------------------------------------------

class FcTarget:
    """A pure-firecracker exec target: podman-shaped like the moca backend's
    MocaTarget (same /testbed -> /workspace/testbed translation and the same
    base64/setpriv wrapper), so trajectories replay identically.

    Self-sufficient by construction (template + workspace dir): the step
    engine runs each unit as a CHILD process (replay_steps shells out to
    `python3 -I replay.py <traj> <spec>`), so the child rebuilds the target
    from the spec alone.  In warm mode the child's target holds its own
    resident VM for the unit's lifetime and closes it on exit; targets used
    driver-side (git-diff, pristine check) default to per-exec VMs so the
    driver never holds residents."""

    _TESTBED = re.compile(r"(?<![\w-])/testbed(?![\w-])")

    def __init__(self, template, workspace_dir, mode=None):
        self.snapshot_dir = os.path.join(FC_SNAPSHOTS, template)
        self.workspace_dir = workspace_dir
        self.workspace_img = os.path.join(workspace_dir, "workspace.img")
        self.mode = mode or FC_VM_MODE
        self.tb = template.startswith("tb-")
        self._vm = None

    def translate(self, text):
        # TB recordings use /app paths, and the template's /app symlinks to
        # the per-session /workspace -- no translation needed. SWE-bench
        # recordings use /testbed, which the moca model relocates.
        if self.tb:
            return text
        return self._TESTBED.sub("/workspace/testbed", text)

    def run(self, script, timeout=1800):
        b64 = base64.b64encode(script.encode()).decode()
        # per-unit shape (REPLAY_FC_UID/WORKDIR/PATH, set by the manifest
        # driver from the unit's sandbox block); defaults are the swebench
        # shape. TB defaults: root, /app (the workspace symlink), plain PATH.
        uid = os.environ.get("REPLAY_FC_UID", "1000")
        workdir = os.environ.get(
            "REPLAY_FC_WORKDIR", "/app" if self.tb else "/workspace/testbed")
        path = os.environ.get(
            "REPLAY_FC_PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            if self.tb else _TESTBED_ENV_PATH)
        # the tty/stdio fidelity trio learned on the VM backend, applied
        # here too: no pager (recordings ran non-tty), HOME for root's
        # gitconfig, and no stdin (a bare `cat` must see EOF, not eat the
        # wrapper's remaining lines)
        home = f" HOME=/root" if uid == "0" else ""
        wrapper = (
            f"echo {b64} | base64 -d > /tmp/.replay-step.sh\n"
            f"exec /usr/bin/setpriv --reuid={uid} --regid={uid} "
            f"--clear-groups "
            f"/usr/bin/env PATH='{path}' "
            f"PAGER=cat GIT_PAGER=cat TERM=dumb{home} "
            f"/bin/bash -c 'cd {workdir} && "
            f"exec bash /tmp/.replay-step.sh < /dev/null'\n")
        t = min(int(timeout), 840)
        if self.mode == "warm":
            # resident VM: restored once, kept alive across steps (warm page
            # cache, no on/off per step).  Execs on one VM are naturally
            # serialized -- one child process per unit, one thread per unit.
            if self._vm is None:
                self._vm = leased_vm(self.snapshot_dir, self.workspace_img).vm
            rc, out, err = self._vm.exec(wrapper, t)
        else:
            with leased_vm(self.snapshot_dir, self.workspace_img) as vm:
                rc, out, err = vm.exec(wrapper, t)
        return subprocess.CompletedProcess([], rc, out, err)

    def close(self):
        """Kill the resident VM (warm mode). The step engine's child calls
        this on exit; without it every unit would leak a firecracker."""
        if self._vm is not None:
            try:
                self._vm.stop()
            finally:
                _release_vm_slot()
                self._vm = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FcSandbox:
    """A pure-firecracker sandbox: a persistent /workspace ext4 image seeded
    from the template's read-only /testbed.  Per exec, a fresh VM restored
    from the golden snapshot (perexec) or the unit's resident VM (warm, held
    by the step child). Root required (jailer)."""

    def __init__(self, name, instance):
        self.name, self.instance = name, instance
        self.template = fc_template(instance)
        self.snapshot_dir = os.path.join(FC_SNAPSHOTS, self.template)
        self.workspace_dir = os.path.join(FC_BASE, "workspaces",
                                          self.name.replace("_", "-"))
        self.workspace_img = os.path.join(self.workspace_dir, "workspace.img")

    def _preflight(self):
        if os.geteuid() != 0:
            raise FcError("the fc backend needs root (jailer chroots): "
                          "run the driver under sudo")
        for path in (FC_FIRECRACKER, FC_JAILER, FC_GUEST_CLIENT):
            if not os.access(path, os.X_OK):
                raise FcError(f"missing {path} (run images/prepare_fc_backend.sh)")
        if not os.path.isdir(self.snapshot_dir):
            raise FcError(f"no golden snapshot at {self.snapshot_dir} "
                          f"(the moca install provides it)")
        if not os.access("/dev/kvm", os.W_OK):
            raise FcError("no /dev/kvm access")

    def start(self):
        self._preflight()
        os.makedirs(self.workspace_dir, exist_ok=True)
        if not os.path.exists(self.workspace_img):
            with open(self.workspace_img, "wb") as f:
                f.truncate(WORKSPACE_IMAGE_BYTES)
            subprocess.run(["mkfs.ext4", "-q", "-F", self.workspace_img],
                           check=True)
        # seed: same as the moca backend -- copy the read-only testbed into
        # the writable workspace, verify the wrapper's prerequisites.  A
        # one-shot VM (synced on stop) is enough: the workspace carries the
        # state, not the VM.  SWE-bench templates carry the tree at /testbed
        # (copied to /workspace/testbed); TB templates moved the environment
        # to /opt/testbed-src and symlink /app -> /workspace, so the seed is
        # the CONTENTS of /opt/testbed-src into /workspace (recorded /app
        # paths then resolve through the symlink).
        if self.template.startswith("tb-"):
            seed = "cp -a /opt/testbed-src/. /workspace/ && test -L /app"
        else:
            seed = "cp -a /testbed /workspace/testbed"
        with leased_vm(self.snapshot_dir, self.workspace_img) as vm:
            rc, out, err = vm.exec(
                seed + " && test -x /usr/bin/setpriv && test -x /usr/bin/env",
                300)
        if rc != 0:
            self.stop()
            raise FcError(f"fc sandbox seed failed (rc={rc}): "
                          f"{(err or out).strip()[-300:]}")

    def target(self):
        # driver-side target (git-diff, pristine check): per-exec VMs, so
        # the driver never holds resident firecrackers
        return FcTarget(self.template, self.workspace_dir, mode="perexec")

    def stop(self):
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    @property
    def target_spec(self):
        # self-sufficient for the step child: template + workspace dir
        return f"fc:{self.template}:{self.workspace_dir}"


class _LeasedVM:
    """An FcVM that releases the module's concurrency slot on exit."""
    def __init__(self, vm):
        self.vm = vm

    def __enter__(self):
        return self.vm

    def __exit__(self, *exc):
        try:
            self.vm.stop()
        finally:
            _release_vm_slot()


def leased_vm(snapshot_dir, workspace_img):
    """A fresh VM on the given workspace, under the concurrency cap."""
    _acquire_vm_slot()
    vm = FcVM(snapshot_dir, workspace_img)
    try:
        vm.start()
    except Exception:
        _release_vm_slot()
        raise
    return _LeasedVM(vm)


# module-wide VM concurrency cap (moca's worker budget cannot see these VMs)
_VM_SEMAPHORE = None


def _semaphore():
    global _VM_SEMAPHORE
    if _VM_SEMAPHORE is None:
        import threading
        _VM_SEMAPHORE = threading.Semaphore(FC_MAX_VMS)
    return _VM_SEMAPHORE


def _acquire_vm_slot():
    _semaphore().acquire()


def _release_vm_slot():
    _semaphore().release()


# --- target lookup for replay.make_target ---------------------------------------

_SANDBOXES = {}


def register_sandbox(sandbox):
    """FcSandbox.start() registers itself so replay.make_target's `fc:` case
    can find the sandbox that owns a workspace dir (the target itself is
    stateless; the VM and workspace carry the state)."""
    _SANDBOXES[os.path.abspath(sandbox.workspace_dir)] = sandbox


def sandbox_for_dir(workspace_dir):
    key = os.path.abspath(workspace_dir)
    if key not in _SANDBOXES:
        raise FcError(f"no live fc sandbox owns {workspace_dir}")
    return _SANDBOXES[key]
