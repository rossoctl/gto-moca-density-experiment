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
import tempfile
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
    backend's moca_template)."""
    return "sweb-" + instance.split("__", 1)[1]


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
        self.jail = os.path.join(FC_BASE, "jail", self.id, self.id)
        self.jail_parent = os.path.dirname(self.jail)
        self.proc = None
        self.console_log = os.path.join(self.jail_parent, "console.log")

    def start(self, api_wait_s=10.0, vsock_wait_s=15.0):
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
        self._api_put(api_sock, "/snapshot/load", """{
            "snapshot_path": "/vmstate",
            "mem_backend": {"backend_path": "/memfile", "backend_type": "File"},
            "vsock_override": {"uds_path": "/run/v.sock"},
            "resume_vm": true
        }""")
        self._wait_file(os.path.join(self.jail, "run", "v.sock"),
                        vsock_wait_s, "vsock socket")

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

    def _api_put(self, unix_sock, path, body):
        """One HTTP PUT over the jail's API socket. Firecracker's API is
        plain HTTP/1.1 on a unix socket; the body is small and fixed."""
        sock_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
        try:
            import socket
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(unix_sock)
            req = (f"PUT {path} HTTP/1.1\r\nHost: localhost\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                   f"{body}")
            s.sendall(req.encode())
            resp = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                resp += chunk
            s.close()
            status = resp.split(b"\r\n", 1)[0].decode("latin-1")
            if " 200" not in status and " 204" not in status:
                raise FcError(f"firecracker API {path}: {status}: "
                              f"{resp[-500:].decode('latin-1')}")
        finally:
            sock_file.close()
            os.unlink(sock_file.name)


# --- the replay Sandbox/Target pair --------------------------------------------

class FcTarget:
    """A pure-firecracker sandbox's exec target: podman-shaped like the moca
    backend's MocaTarget (same /testbed -> /workspace/testbed translation and
    the same base64/setpriv wrapper), so trajectories replay identically."""

    _TESTBED = re.compile(r"(?<![\w-])/testbed(?![\w-])")

    def __init__(self, sandbox):
        self.sandbox = sandbox

    def translate(self, text):
        return self._TESTBED.sub("/workspace/testbed", text)

    def run(self, script, timeout=1800):
        b64 = base64.b64encode(script.encode()).decode()
        wrapper = (
            f"echo {b64} | base64 -d > /tmp/.replay-step.sh\n"
            f"exec /usr/bin/setpriv --reuid=1000 --regid=1000 --clear-groups "
            f"/usr/bin/env PATH='{_TESTBED_ENV_PATH}' "
            f"/bin/bash -c 'cd /workspace/testbed && "
            f"exec bash /tmp/.replay-step.sh'\n")
        t = min(int(timeout), 840)
        if FC_VM_MODE == "warm":
            # the sandbox's resident VM: restored once, kept alive across
            # steps (warm page cache, no on/off per step), killed at stop().
            # Execs on one VM are naturally serialized -- the replay drivers
            # run one thread per sandbox.
            vm = self.sandbox.resident_vm()
            rc, out, err = vm.exec(wrapper, t)
        else:
            with self.sandbox.vm() as vm:
                rc, out, err = vm.exec(wrapper, t)
        return subprocess.CompletedProcess([], rc, out, err)


class FcSandbox:
    """A pure-firecracker sandbox: a persistent /workspace ext4 image seeded
    from the template's read-only /testbed, driven per exec by a fresh VM
    restored from the golden snapshot, then killed. Root required (jailer)."""

    def __init__(self, name, instance):
        self.name, self.instance = name, instance
        self.snapshot_dir = os.path.join(FC_SNAPSHOTS, fc_template(instance))
        self.workspace_dir = os.path.join(FC_BASE, "workspaces",
                                          self.name.replace("_", "-"))
        self.workspace_img = os.path.join(self.workspace_dir, "workspace.img")
        self._resident = None   # FC_VM_MODE=warm's long-lived VM

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
        # seed: same as the moca backend — copy the read-only testbed into
        # the writable workspace, verify the wrapper's prerequisites
        with self.vm() as vm:
            rc, out, err = vm.exec(
                "cp -a /testbed /workspace/testbed && "
                "test -x /usr/bin/setpriv && test -x /usr/bin/env", 300)
        if rc != 0:
            self.stop()
            raise FcError(f"fc sandbox seed failed (rc={rc}): "
                          f"{(err or out).strip()[-300:]}")

    def vm(self):
        """A fresh VM on this sandbox's workspace, under the concurrency cap."""
        _acquire_vm_slot()
        vm = FcVM(self.snapshot_dir, self.workspace_img)
        started = False
        try:
            vm.start()
            started = True
        finally:
            if not started:
                _release_vm_slot()
        return _LeasedVM(vm)

    def resident_vm(self):
        """The sandbox's long-lived VM (FC_VM_MODE=warm): restored on first
        use, held for the sandbox's life.  Holds its concurrency slot for
        that whole life -- warm mode trades the per-exec restore for
        resident memory, which is the thing being measured."""
        if self._resident is None:
            _acquire_vm_slot()
            vm = FcVM(self.snapshot_dir, self.workspace_img)
            try:
                vm.start()
            except Exception:
                _release_vm_slot()
                raise
            self._resident = vm
        return self._resident

    def target(self):
        return FcTarget(self)

    def stop(self):
        if self._resident is not None:
            try:
                self._resident.stop()
            finally:
                _release_vm_slot()
                self._resident = None
        shutil.rmtree(self.workspace_dir, ignore_errors=True)

    @property
    def target_spec(self):
        return f"fc:{self.workspace_dir}"


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
