#!/usr/bin/env python3 -I
"""Replay a container-hands trajectory against a podman container or a VM.

Usage: python3 -I replay.py <trajectory.json> <target> [timeline.txt]

Targets (see USAGE.md for the full story):
  <container-name>     running podman container (pin-compatible with the
                       original replay_r1.sh flow)
  podman:<name>        same thing, explicit
  vm:<socket>          a VM booted by `replay.py vm-start`, spoken to over
                       its serial-console socket
  qemu:<vm-dir>        ephemeral: boot the VM in <vm-dir> (as produced by
                       images/to_qcow2.sh), run this one operation, shut down

Other modes:
  vm-start <vm-dir> <socket>   start a VM (resumes from its saved state
                               if it has any), wait for its console
  vm-snapshot <vm-dir>         save the VM state file (prep stage; runs
                               under KVM like the loads)
  vm-stop <socket>             shut a booted VM down
  git-status <target>          print `git status --porcelain` in /testbed
  git-diff <target>            print `git diff HEAD --binary` in /testbed

Drives the target with the recorded uid (1000), workdir /testbed and the
task's python first on PATH, the way the agent's own tools ran there. Model
steps are skipped (they change no files). For each tool step it records the
replay's exit code / outcome and prints one line per step; the replay exits 0
iff every recorded step succeeded again.

With a timeline file (the run's .timeline.txt), replay is timed: each tool
step fires at its recorded `at` time relative to the start of the replay (the
model-call gaps are simply waited out). If a command overruns its slot, the
timeline slides: every later step shifts by the overrun instead of the replay
falling behind its own clock. A per-step report and a timing summary are
printed at the end.

Exit-code note: container trajectories record only is_error (all false after
failed-call removal) and no per-command exit codes, so "exit code matches the
recording" here means: every Bash exits 0, every Read finds its range, every
Edit applies exactly once.
"""
import base64, contextlib, glob, json, os, re, select, signal, socket, subprocess, sys, time

# the image activates the testbed conda env only in root's .bashrc; the agent
# ran as uid 1000 with this on its PATH, so the replay sets it explicitly.
PATH = "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Read/Edit tool semantics, inlined so no files need to reach the guest:
# the source below plus the step's spec is piped to `python -` inside the
# target.   Read: exit 0 iff the file exists (and the range exists).
#   Edit: exit 0 iff old_string occurs exactly once and the file changes.
EDIT_TOOL = '''
import sys

def run_spec(spec):
    path = spec["file_path"]

    if spec["op"] == "read":
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        offset = spec.get("offset")
        limit = spec.get("limit")
        if offset is not None:
            start = max(0, offset - 1)  # offset is 1-based
            end = len(lines) if limit is None else start + limit
            sel = lines[start:end]
            if not sel and offset > len(lines):
                print(f"read {path}: offset {offset} past EOF ({len(lines)} lines)", file=sys.stderr)
                return 1
        return 0

    if spec["op"] == "edit":
        with open(path, encoding="utf-8") as f:
            text = f.read()
        old, new = spec["old_string"], spec["new_string"]
        n = text.count(old)
        if n != 1:
            print(f"edit {path}: old_string occurs {n} times (need exactly 1)", file=sys.stderr)
            return 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new, 1))
        return 0

    print(f"unknown op {spec['op']!r}", file=sys.stderr)
    return 1
'''

# markers must match at a line start, but readline decorates the line before
# command output with CSI sequences (bracketed-paste off) and a \r, so allow
# that prefix. echoed command text ("... # echo __X__") still can't match.
MARKER_PREFIX = "(?:\\x1b\\[[0-9;?]*[a-zA-Z])*\\r?"

def marker_rx(body):
    return f"(?m)^{MARKER_PREFIX}{body}"

class Result:
    """What run() returns: same attrs as subprocess.CompletedProcess."""
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""

class PodmanTarget:
    """A running podman container, driven with `podman exec`."""
    def __init__(self, container):
        self.container = container

    def translate(self, text):
        """Recorded steps reference /testbed absolutely; on this backend
        that is the workdir, so no translation."""
        return text

    def run(self, script, timeout=1800):
        return subprocess.run(
            ["podman", "exec", "-i", "-e", f"PATH={PATH}",
             "-u", "1000:1000", "-w", "/testbed", self.container, "bash", "-s"],
            input=script, capture_output=True, text=True, timeout=timeout)

class Console:
    """One client of a VM's serial-console unix socket."""
    def __init__(self, sock_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)     # a server that stops accepting must not
        self.sock.connect(sock_path) # hang the replay forever
        self.sock.setblocking(False)
        self.buf = b""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, text):
        self.sock.setblocking(True)
        self.sock.sendall(text.encode())
        self.sock.setblocking(False)

    def read_until(self, pattern, timeout):
        """Consume (and return) everything through the first `pattern` match;
        anything after it stays buffered for the next call."""
        rx = re.compile(pattern.encode())
        deadline = time.monotonic() + timeout
        while True:
            m = rx.search(self.buf)
            if m:
                consumed, self.buf = self.buf[:m.end()], self.buf[m.end():]
                return consumed.decode(errors="replace")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"console: no match for {pattern!r} "
                                   f"within {timeout}s (buffer tail: "
                                   f"{self.buf[-200:]!r})")
            r, _, _ = select.select([self.sock], [], [], min(remaining, 1.0))
            if r:
                data = self.sock.recv(65536)
                if not data:
                    raise ConnectionError("console socket closed by the VM")
                self.buf += data

class VmTarget:
    """A VM booted by vm-start, driven through its serial console.

    The guest runs init=/bin/bash on ttyS0 (see vm_start). Each run() ships
    the script base64-encoded (no quoting games with recorded commands),
    executes it as uid 1000 in /testbed via setpriv, and frames the output
    between random begin/end markers so console noise (kernel messages,
    prompts) never leaks into step output.
    """
    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.con = Console(sock_path)
        self._sync()

    def translate(self, text):
        """Recorded steps reference /testbed absolutely; on this backend
        that is the workdir, so no translation."""
        return text

    def _sync(self):
        for _ in range(3):
            tok = os.urandom(6).hex()
            try:
                self.con.send(f"\necho __S{tok}__\n")
                self.con.read_until(marker_rx(f"__S{tok}__\\r?$"), timeout=10)
                return
            except (TimeoutError, ConnectionError, OSError):
                self.con.close()
                self.con = Console(self.sock_path)
        raise ConnectionError(f"console at {self.sock_path} "
                              f"is not answering")

    def run(self, script, timeout=1800):
        # the whole wrapper travels as ONE base64 line: if the tty echoes
        # input, the only echoed text is that marker-free line, and the
        # begin/end markers are line-anchored so echoed text can't match them
        tok = os.urandom(8).hex()
        b64 = base64.b64encode(script.encode()).decode()
        wrapper = (
            f"echo __B{tok}__\n"
            f"echo {b64} | base64 -d > /tmp/.replay-step.sh\n"
            f"/usr/bin/setpriv --reuid=1000 --regid=1000 --clear-groups "
            f"/usr/bin/env PATH='{PATH}' "
            f"/bin/bash -c 'cd /testbed && exec bash /tmp/.replay-step.sh' 2>&1\n"
            f"echo __R{tok}__=$?\n")
        outer = base64.b64encode(wrapper.encode()).decode()
        try:
            self.con.send(f"echo {outer} | base64 -d | bash\n")
            # the end marker needs NO line anchor: a step whose output has no
            # trailing newline (e.g. `base64 -w0`) glues the marker onto its
            # last line, and base64's alphabet has no underscores, so the
            # marker text can only be the wrapper's own echo
            end_pat = f"(?m)__R{tok}__=(\\d+)\\r?$"
            consumed = self.con.read_until(end_pat, timeout)
        except (TimeoutError, ConnectionError, OSError) as e:
            return Result(124, f"replay: VM step failed: {e}")
        end = re.search(end_pat, consumed)
        rc = int(end.group(1))
        begin = re.search(marker_rx(f"__B{tok}__\\r?$"), consumed)
        out = consumed[begin.end():end.start()] if begin else consumed
        if out.startswith("\n"):      # the BEGIN line's own newline ($ stops
            out = out[1:]             # just before it)
        return Result(rc, out.replace("\r\n", "\n"))

class EphemeralVm:
    """`qemu:<vm-dir>` target: boot, run one operation, shut down."""
    def __init__(self, vm_dir):
        self.sock = os.path.join("/tmp", f"replay-vm-{os.getpid()}.sock")
        vm_start(vm_dir, self.sock)

    def __enter__(self):
        return VmTarget(self.sock)

    def __exit__(self, *exc):
        vm_stop(self.sock)
        return False

def make_target(spec):
    if spec.startswith("podman:"):
        return PodmanTarget(spec[7:])
    if spec.startswith("vm:"):
        return VmTarget(spec[3:])
    if spec.startswith("moca:"):
        return MocaTarget(spec[5:])
    if spec.startswith("qemu:"):
        return EphemeralVm(spec[5:])   # context manager, not a target
    return PodmanTarget(spec)

# --- VM lifecycle ------------------------------------------------------------

QEMU_APPEND = "root=/dev/vda rw console=ttyS0 init=/bin/bash"
# Full VM state (RAM + devices) saved by vm_snapshot next to the qcow2.
# Not an internal qemu snapshot: -loadvm cannot see through a snapshot=on
# overlay, so runs resume via incoming migration from this file instead --
# memory file + pristine base + throwaway overlay, the microVM model.
STATE_FILE = "state.bin"

def _kvm_preflight(what):
    """VMs are KVM-only: memory snapshots are accelerator-pinned (a KVM-saved
    state cannot restore under TCG), and TCG timings would poison load runs.
    REPLAY_VM_ALLOW_TCG=1 exists for development on kvm-less shells, where
    save and load must then both run under TCG."""
    if not os.access("/dev/kvm", os.W_OK) and os.environ.get("REPLAY_VM_ALLOW_TCG") != "1":
        raise SystemExit(
            f"{what}: no KVM access in this shell (/dev/kvm not writable).\n"
            "      Run under KVM with:  sg kvm -c '<command>'\n"
            "      (group membership only reaches new processes; a long-lived\n"
            "       window manager's terminals keep the old groups)")

def _launch(vm_dir, sock, overlay=True, incoming=None):
    """Launch the VM daemonized: console and monitor on unix sockets next to
    `sock`. overlay=True (the run case) puts all writes on a throwaway
    snapshot overlay; overlay=False lets writes reach the base image (only
    vm_snapshot does that, for the setup the saved state assumes). incoming
    resumes from a vm_snapshot state file instead of booting."""
    qcow2 = _sole(glob.glob(f"{vm_dir}/*.qcow2"), "qcow2 image", vm_dir)
    kernel = _sole(glob.glob(f"{vm_dir}/vmlinuz-*"), "kernel", vm_dir)
    initrd = _sole(glob.glob(f"{vm_dir}/initrd*"), "initrd", vm_dir)
    pidfile = sock + ".pid"
    if os.path.exists(sock):
        _raise_if_running(sock, pidfile)
        os.unlink(sock)
    drive = f"file={qcow2},if=virtio,format=qcow2"
    if overlay:
        drive += ",snapshot=on"
    cmd = ["qemu-system-x86_64",
           "-machine", "accel=kvm:tcg", "-m", "2048", "-smp", "2",
           "-kernel", kernel, "-initrd", initrd, "-append", QEMU_APPEND,
           "-drive", drive,
           "-display", "none", "-net", "none",
           "-serial", f"unix:{sock},server=on,wait=off",
           "-monitor", f"unix:{sock}.mon,server=on,wait=off",
           "-daemonize", "-pidfile", pidfile]
    if incoming:
        cmd += ["-incoming", f"exec:cat {incoming}"]
    subprocess.run(cmd, check=True)
    return qcow2

def _wait_shell(sock, deadline_s, vm_dir):
    """Wait for the guest shell: early input is lost during boot, so poke the
    console with a marker until it answers. The marker is line-anchored so
    the tty's echo of the probe itself can't match it. (The chardev serves
    one client: close before retrying; connection resets mean not-ready.)"""
    deadline = time.monotonic() + deadline_s
    con = None
    while time.monotonic() < deadline:
        try:
            if con is None:
                con = Console(sock)
            tok = os.urandom(6).hex()
            con.send(f"\necho __RDY{tok}__\n")
            con.read_until(marker_rx(f"__RDY{tok}__\\r?$"), timeout=5)
            return con
        except (TimeoutError, ConnectionError, ConnectionResetError, OSError):
            if con is not None:
                con.close()
                con = None
            time.sleep(1)
    vm_stop(sock)
    raise TimeoutError(f"VM in {vm_dir} did not reach a shell "
                       f"within {deadline_s}s")

def _run_setup(con, vm_dir, sock):
    """Bring the freshly booted guest to the state every step assumes.
    Replicates what a container runtime provides implicitly: /dev/shm
    (python multiprocessing semaphores) and /run; init=/bin/bash mounts
    neither (/proc and /sys come from the initramfs). Also silences kernel
    printks (the crng-init message ~25s after boot would pollute step
    output and could corrupt the base64 patch stream) and console echo.
    Setup travels the same way steps do -- one base64 line through the
    pipe -- because long raw lines get mangled by readline's interactive
    echo. It ends with a marker carrying the mounts' exit code so failures
    are loud."""
    stok = os.urandom(6).hex()
    setup = (
        "dmesg -n 1 2>/dev/null || true\n"
        "stty -echo 2>/dev/null\n"
        "export PS1=''\n"
        "mkdir -p /dev/shm /run\n"
        "mountpoint -q /dev/shm || mount -t tmpfs -o mode=1777 tmpfs /dev/shm\n"
        "mountpoint -q /run || mount -t tmpfs tmpfs /run\n"
        f"echo __SETUP{stok}__=$?\n")
    sb64 = base64.b64encode(setup.encode()).decode()
    con.send(f"echo {sb64} | base64 -d | bash\n")
    try:
        got = con.read_until(f"__SETUP{stok}__=(\\d+)", timeout=120)
    except (TimeoutError, ConnectionError) as e:
        vm_stop(sock)
        raise TimeoutError(f"VM in {vm_dir}: console setup did not "
                           f"complete: {e}") from e
    rc = int(re.search(f"__SETUP{stok}__=(\\d+)", got).group(1))
    if rc != 0:
        vm_stop(sock)
        raise SystemExit(f"guest setup failed (mounts rc={rc}) in {vm_dir}; "
                         f"see the console protocol in USAGE.md")

def vm_start(vm_dir, sock):
    """Start the VM in vm_dir with its console on `sock`. If vm_snapshot's
    state file is present (normally saved by to_qcow2.sh), resume from it:
    no boot, no setup -- sub-second to a synced shell -- with the run's
    writes still on a throwaway overlay so the base stays pristine."""
    _kvm_preflight("vm-start")
    vm_dir = os.path.abspath(vm_dir)
    state = os.path.join(vm_dir, STATE_FILE)
    resumed = os.path.exists(state)
    _launch(vm_dir, sock, overlay=True, incoming=state if resumed else None)
    con = _wait_shell(sock, 60 if resumed else 180, vm_dir)
    if not resumed:
        _run_setup(con, vm_dir, sock)
    con.close()

def vm_snapshot(vm_dir):
    """Prep stage (normally the last step of to_qcow2.sh): boot the VM the
    long way, run the standard setup (writing the base image -- the only
    code path that does), then save full VM state to STATE_FILE via
    migration. Must run under the same accelerator the state will be
    resumed under (KVM in production; TCG pairs with TCG)."""
    _kvm_preflight("vm-snapshot")
    vm_dir = os.path.abspath(vm_dir)
    state = os.path.join(vm_dir, STATE_FILE)
    sock = f"/tmp/replay-prep-{os.getpid()}.sock"
    _launch(vm_dir, sock, overlay=False)
    try:
        con = _wait_shell(sock, 180, vm_dir)
        _run_setup(con, vm_dir, sock)
        con.close()
        mon = Console(sock + ".mon")
        mon.read_until(r"\(qemu\)", timeout=30)      # drain the prompt
        mon.send(f'migrate "exec:cat > {state}"\n')
        # migrate is async: poll until the state file is fully written
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            mon.send("info migrate\n")
            got = mon.read_until(r"\(qemu\)", timeout=30)
            if "Migration status: completed" in got:
                break
            if "Migration status: failed" in got:
                raise RuntimeError(f"state save failed: {got[-300:]}")
            time.sleep(1)
        else:
            raise TimeoutError("state save did not complete within 600s")
        mon.send("quit\n")
        mon.close()
        pid = int(open(sock + ".pid").read().strip())
        for _ in range(300):
            if not os.path.exists(f"/proc/{pid}"):
                break
            time.sleep(0.1)
    finally:
        vm_stop(sock)          # cleans up; SIGTERMs if quit left it alive
    print(f"VM state saved to {state}")

def vm_stop(sock):
    pidfile = sock + ".pid"
    try:
        pid = int(open(pidfile).read().strip())
    except (OSError, ValueError):
        pid = None
    if pid and os.path.exists(f"/proc/{pid}"):
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not os.path.exists(f"/proc/{pid}"):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    for f in (pidfile, sock):
        try:
            os.unlink(f)
        except OSError:
            pass

def _sole(matches, what, where):
    if len(matches) != 1:
        raise SystemExit(f"need exactly one {what} in {where}, found {matches}")
    return matches[0]

def _raise_if_running(sock, pidfile):
    try:
        Console(sock).sock.close()
        raise SystemExit(f"a VM is already listening on {sock}; "
                         f"vm-stop it first")
    except (FileNotFoundError, ConnectionRefusedError):
        pass

# --- sandboxes: backend lifecycle for drivers --------------------------------
#
# Drivers (replay_trajectory.py, replay_manifest.py) use these instead of
# knowing how a backend is started or stopped. The CLI modes below (vm-start
# etc.) are thin wrappers over the same machinery.

class PodmanSandbox:
    """A podman container sandbox around a PodmanTarget."""
    def __init__(self, name, image):
        self.name, self.image = name, image

    def start(self):
        subprocess.run(["podman", "rm", "-f", self.name], capture_output=True)
        p = subprocess.run(["podman", "run", "-d", "--name", self.name,
                            "--user", "1000:1000", "-w", "/testbed",
                            self.image, "sleep", "infinity"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            # stderr is the diagnosis (e.g. the ctr host's inotify-instance
            # exhaustion surfaced only as a bare "exit status 126")
            raise RuntimeError(f"podman run failed ({p.returncode}): "
                               f"{p.stderr.strip()[-400:]}")

    def target(self):
        return PodmanTarget(self.name)

    def stop(self):
        subprocess.run(["podman", "rm", "-f", self.name], capture_output=True)

    @property
    def target_spec(self):
        return self.name            # bare name: the pin-compatible CLI form

class VmSandbox:
    """A QEMU VM sandbox around a VmTarget: own console socket, resumed from
    the prep state file when present, writes on a throwaway overlay."""
    def __init__(self, name, vm_dir):
        self.name, self.vm_dir = name, os.path.abspath(vm_dir)
        self.sock = f"/tmp/replay-vm-{self.name}.sock"

    def start(self):
        vm_start(self.vm_dir, self.sock)

    def target(self):
        return VmTarget(self.sock)

    def stop(self):
        vm_stop(self.sock)

    @property
    def target_spec(self):
        return f"vm:{self.sock}"

def _moca_exec(sandbox_id, script, timeout, retries=5):
    """Run a bash script on stdin in a moca sandbox via mocactl. Each exec
    boots a fresh Firecracker microVM over the persistent /workspace. Exit
    codes are the command's own; 124 = mocactl timeout, 125 = sandbox
    failure. mocactl caps --timeout at 840s (token lifetime minus 60s).

    "template busy; retry later" (exit 125) is the shared KVM host's
    standby-VM pool for the template running dry under concurrency -- a
    PRE-EXEC refusal (the command never started), so it is safe to retry
    with backoff. Other 125s propagate unchanged."""
    t = min(int(timeout), 840)
    delays = [2, 5, 10, 20, 40]
    for attempt in range(retries + 1):
        p = subprocess.run(
            ["mocactl", "sbx", "run", "--sandbox", sandbox_id,
             "--timeout", str(t)],
            input=script, capture_output=True, text=True, timeout=t + 120)
        if p.returncode != 125 or attempt == retries:
            return p
        if "template busy" not in (p.stderr or ""):
            return p
        time.sleep(delays[min(attempt, len(delays) - 1)])
    return p

class MocaTarget:
    """A moca sandbox, driven with `mocactl sbx run`: podman-shaped (direct
    output capture, exit codes propagate), so no marker protocol. Steps run
    as uid 1000 in /workspace/testbed (the read-only /testbed seed is copied
    there by MocaSandbox.start)."""
    # recorded trajectories reference /testbed absolutely; on moca the
    # writable copy lives at /workspace/testbed, so translate paths
    _TESTBED = re.compile(r"(?<![\w-])/testbed(?![\w-])")

    def __init__(self, sandbox_id):
        self.sandbox_id = sandbox_id

    def translate(self, text):
        return self._TESTBED.sub("/workspace/testbed", text)

    def run(self, script, timeout=1800):
        b64 = base64.b64encode(script.encode()).decode()
        wrapper = (
            f"echo {b64} | base64 -d > /tmp/.replay-step.sh\n"
            f"exec /usr/bin/setpriv --reuid=1000 --regid=1000 --clear-groups "
            f"/usr/bin/env PATH='{PATH}' "
            f"/bin/bash -c 'cd /workspace/testbed && "
            f"exec bash /tmp/.replay-step.sh'\n")
        return _moca_exec(self.sandbox_id, wrapper, timeout)

def moca_template(instance):
    """SWE-bench instance id -> moca template id:
    django__django-10914 -> sweb-django-10914, pydata__xarray-4094 ->
    sweb-xarray-4094 (template ids allow no underscores)."""
    return "sweb-" + instance.split("__", 1)[1]

class MocaSandbox:
    """A moca sandbox: created from a per-instance template, seeded (the
    microVM tier's /testbed is a read-only rootfs snapshot, so the replay
    works on a copy in the persistent /workspace), driven per exec, then
    removed. Sandboxes are stateful ONLY in /workspace; every exec is a
    fresh VM, like a container whose filesystem persists between commands."""
    def __init__(self, name, instance):
        self.name, self.instance = name, instance
        self.sandbox_id = None

    def start(self):
        p = subprocess.run(
            ["mocactl", "sbx", "create", "--template", moca_template(self.instance),
             "--name", self.name.replace("_", "-"), "--json"],
            capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"mocactl sbx create failed: {p.stderr.strip()}")
        self.sandbox_id = json.loads(p.stdout)["id"]
        # seed: copy the read-only testbed into the writable workspace, and
        # fail loudly if the step wrapper's prerequisites are missing
        r = _moca_exec(self.sandbox_id,
                       "cp -a /testbed /workspace/testbed && "
                       "test -x /usr/bin/setpriv && test -x /usr/bin/env",
                       timeout=300)
        if r.returncode != 0:
            self.stop()
            raise RuntimeError(f"moca sandbox seed failed (rc={r.returncode}): "
                               f"{(r.stderr or r.stdout).strip()[-300:]}")

    def target(self):
        return MocaTarget(self.sandbox_id)

    def stop(self):
        if self.sandbox_id:
            subprocess.run(["mocactl", "sbx", "rm", self.sandbox_id, "--force"],
                           capture_output=True)

    @property
    def target_spec(self):
        return f"moca:{self.sandbox_id}"

SANDBOXES = {"podman": PodmanSandbox, "vm": VmSandbox, "moca": MocaSandbox}

# --- shared driver helpers ----------------------------------------------------

def git_status_porcelain(target):
    """`git status --porcelain` in /testbed; empty string means pristine."""
    return target.run("git status --porcelain").stdout

def git_diff_binary(target):
    """`git diff HEAD --binary` in /testbed as bytes (base64-tunneled on VM
    targets so binary hunks survive the console stream)."""
    p = target.run("git -c user.name=replay -c user.email=replay@invalid "
                   "diff HEAD --binary | base64 -w0")
    if p.returncode != 0:
        raise RuntimeError(f"git-diff failed: {(p.stderr or p.stdout)[-400:]}")
    return base64.b64decode(p.stdout)

def patch_matches(got, recorded):
    """Byte-exact patch compare, falling back to index-line-insensitive
    (blob hashes differ by design between base and baseline commits)."""
    if got == recorded:
        return True
    norm = lambda b: [l for l in b.decode("latin-1").splitlines()
                      if not l.startswith("index ")]
    return norm(got) == norm(recorded)

def replay_steps(traj_path, target_spec, timeline=None, stdout=None):
    """Run the step engine as a subprocess (isolation, per-unit logs);
    returns its exit code (0 iff every recorded step succeeded again)."""
    cmd = [sys.executable, "-I", os.path.abspath(__file__),
           traj_path, target_spec]
    if timeline:
        cmd.append(timeline)
    return subprocess.run(cmd, stdout=stdout or sys.stdout,
                          stderr=subprocess.STDOUT).returncode

# --- step execution ----------------------------------------------------------

def exec_edit_tool(target, spec, timeout=1800):
    """Run the inlined Read/Edit tool in the target. The program (tool source
    plus the spec as a literal) travels base64-encoded inside the step script
    and is piped to `python -`, so no trajectory text lands in a shell command
    line or heredoc."""
    program = EDIT_TOOL + f"sys.exit(run_spec({spec!r}))\n"
    b64 = base64.b64encode(program.encode()).decode()
    return target.run(f"echo {b64} | base64 -d | exec python -", timeout)

def parse_timeline(path):
    """[(at_ms, dur_ms)] for the TOOL lines of a .timeline.txt, in order."""
    steps = []
    for line in open(path):
        m = re.match(r"^\s*([\d.]+)s\s+([\d.]+)s\s+TOOL\b", line)
        if m:
            steps.append((int(float(m.group(1)) * 1000), int(float(m.group(2)) * 1000)))
    return steps

def run_replay(traj, target, times=None):
    results = []
    t0 = time.monotonic()          # replay's zero, like the run's at_ms=0
    slide_ms = 0                   # cumulative overrun pushed onto later steps
    for i, step in enumerate(traj["steps"]):
        if step["kind"] != "tool":
            continue
        idx = len(results)
        tool, recorded_err = step["tool"], step.get("is_error", False)
        at = step.get("at_ms")
        rec_at, rec_dur = times[idx] if times else (at, None)

        # wait for the recorded start (model-call gaps included), slid by any
        # overrun accumulated so far
        target_ms = rec_at + slide_ms
        wait = target_ms - (time.monotonic() - t0) * 1000
        if wait > 0:
            time.sleep(wait / 1000)

        start_off = (time.monotonic() - t0) * 1000
        if tool == "Bash":
            # the recorded command is the whole script, on stdin to keep quoting
            p = target.run(target.translate(step["command"]))
            outcome, replay_err = f"exit={p.returncode}", p.returncode != 0
            detail = (p.stderr or p.stdout)[-400:].strip()
        elif tool in ("Read", "Edit"):
            spec = dict(step["input"], op=tool.lower())
            spec["file_path"] = target.translate(spec["file_path"])
            p = exec_edit_tool(target, spec)
            outcome, replay_err = f"exit={p.returncode}", p.returncode != 0
            detail = (p.stderr or p.stdout).strip()[:400]
        else:
            outcome, replay_err, detail = "skipped", False, f"unknown tool {tool}"
            p = None

        mismatch = recorded_err != replay_err
        results.append((tool, replay_err))
        status = "MISMATCH" if mismatch else ("FAIL" if replay_err else "ok")

        end_off = (time.monotonic() - t0) * 1000
        dur_ms = end_off - start_off
        timing = ""
        if times:
            # how late this step started vs its (slid) target, and whether it
            # overran its recorded duration (which slides the rest)
            late = start_off - target_ms
            overrun = dur_ms - rec_dur if rec_dur is not None else 0
            if overrun > 0:
                slide_ms += overrun
            timing = (f" rec={rec_at/1000:7.1f}s+{rec_dur/1000 if rec_dur is not None else 0:5.1f}s"
                      f" actual={start_off/1000:7.1f}s+{dur_ms/1000:5.1f}s"
                      f" late={late/1000:+5.1f}s"
                      + (f" slide+{overrun/1000:.1f}s" if overrun > 0 else ""))
        print(f"[{i:3}] {at:7d}ms {tool:5} recorded_error={recorded_err} "
              f"replay {outcome} -> {status}{timing}"
              + (f" | {detail}" if detail and replay_err else ""))

    n_fail = sum(1 for _, e in results if e)
    print(f"\ntool steps replayed: {len(results)}, failed on replay: {n_fail}")
    if times:
        makespan = (time.monotonic() - t0)
        print(f"timed replay: makespan {makespan:.1f}s "
              f"(recorded turn {(traj['turn_ms'])/1000:.1f}s), "
              f"total slide {slide_ms/1000:.1f}s")
    return n_fail

# --- helper modes ------------------------------------------------------------

def run_git_status(target_spec):
    with _maybe_ephemeral(target_spec) as target:
        p = target.run("git status --porcelain")
        sys.stdout.write(p.stdout)
        return p.returncode

def run_git_diff(target_spec):
    with _maybe_ephemeral(target_spec) as target:
        try:
            sys.stdout.buffer.write(git_diff_binary(target))
            return 0
        except RuntimeError as e:
            sys.stderr.write(str(e))
            return 1

@contextlib.contextmanager
def _maybe_ephemeral(target_spec):
    target = make_target(target_spec)
    if isinstance(target, EphemeralVm):
        with target as t:                   # boot, run, shut down
            yield t
    else:
        yield target

USAGE = """\
replay.py -- replay recorded trajectories against containers or VMs

  replay.py TRAJECTORY.json TARGET [TIMELINE.txt]   replay a trajectory
  replay.py vm-start VM_DIR SOCKET                  start a VM (state resume)
  replay.py vm-snapshot VM_DIR                      save the VM state file
  replay.py vm-stop SOCKET                          shut a booted VM down
  replay.py git-status TARGET                       pristine check (porcelain)
  replay.py git-diff TARGET                         post-run patch, on stdout

TARGET selects where steps run:
  <container>      a running podman container (the original replay_r1.sh form)
  podman:<name>    the same, spelled out
  vm:<socket>      a VM booted by `vm-start`; persists across invocations,
                   like a container does, until `vm-stop`
  qemu:<vm-dir>    boot, run this ONE operation, shut down again

See USAGE.md for the full manual.
"""

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return
    if args[0] == "vm-start" and len(args) == 3:
        vm_start(args[1], args[2])
        return
    if args[0] == "vm-stop" and len(args) == 2:
        vm_stop(args[1])
        return
    if args[0] == "vm-snapshot" and len(args) == 2:
        vm_snapshot(args[1])
        return
    if args[0] == "git-status" and len(args) == 2:
        sys.exit(run_git_status(args[1]))
    if args[0] == "git-diff" and len(args) == 2:
        sys.exit(run_git_diff(args[1]))

    traj_path, target_spec = args[0], args[1]
    timeline_path = args[2] if len(args) > 2 else None
    traj = json.load(open(traj_path))
    assert traj["hands"] == "local", "this replayer handles container-hands runs"

    tool_steps = [s for s in traj["steps"] if s["kind"] == "tool"]
    times = None
    if timeline_path:
        times = parse_timeline(timeline_path)
        assert len(times) == len(tool_steps), (
            f"timeline has {len(times)} TOOL lines but trajectory has "
            f"{len(tool_steps)} tool steps")

    with _maybe_ephemeral(target_spec) as target:
        n_fail = run_replay(traj, target, times)
    sys.exit(1 if n_fail else 0)

if __name__ == "__main__":
    main()
