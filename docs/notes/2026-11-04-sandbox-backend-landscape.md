# Sandbox backend landscape — design constraints for harness-separated sandboxes

**What this note is for:** compiled research (web sources, 2026-11-03) supporting a slide on
why microservice-era VM runtimes (Kata) are unsuitable for long-running-but-sparsely-used AI
agent sandboxes, and how the current generation of sandbox providers (E2B, Vercel, Modal,
Daytona, Fly, Anthropic) delivers them instead. Cross-referenced with this repo's own
measurements where they bear on the same tradeoffs. Point-in-time record; vendor numbers age.

---

## 1. The design constraints when sandbox ≠ harness

When the agent loop and the code-execution environment are separate systems, the sandbox
backend is judged on axes a normal container runtime never optimizes for:

1. **Lifecycle primitives beyond start/stop.** The load-bearing operations are
   `pause() / resume() / snapshot() / fork()`. Agent sessions are long-running but sparse:
   most wall-clock time is spent waiting on the LLM, not executing code. Vercel's own pricing
   example assumes **10 % CPU utilization** for an "AI code validation" workload — the same
   ~10 % duty cycle our load50/load80 manifests model. A backend without pause/resume forces
   either a kept-warm VM (paying for idle) or destroying state and re-installing dependencies
   every turn.
2. **Billing for idle.** Every serious provider bills active CPU + provisioned memory, not
   wall-clock: Vercel $0.128/active-CPU-h (I/O-wait and LLM-call waits don't count) +
   $0.0212/GB-h memory; E2B $0.0504/vCPU-h only while running, paused = no compute charge;
   Fly Sprites stops compute billing ~30 s after activity ends.
3. **Latency to first tool call.** Resume latency matters more than cold boot: E2B resume
   ~1 s (pause ~4 s/GiB RAM); Fly warm wake 100–500 ms, cold 1–2 s; Daytona claims <90 ms
   (vendor claim, no published methodology); Modal memory snapshots cut `import torch`
   5 s → 1.05 s p50.
4. **Density per host.** Idle-but-resident sandboxes make per-sandbox memory overhead the
   dominant cost driver. Firecracker's design goal is exactly this: <5 MiB VMM overhead per
   microVM, demand paging and CPU oversubscription on by default.
5. **Statefulness/checkpointing.** Full-state snapshots (memory + filesystem + running
   processes) let a sparse sandbox hibernate between turns: E2B preserves live processes and
   loaded variables; Vercel auto-snapshots the filesystem on stop and resumes the next
   session from it; Modal snapshots expire (7 d memory / 30 d filesystem). The snapshot
   mechanism is a first-class design choice, not an afterthought.
6. **Per-sandbox network isolation.** Untrusted LLM-generated code needs egress policy per
   sandbox, not per pod: Vercel gives each sandbox its own network namespace behind a
   TLS-terminating firewall with a per-sandbox proxy CA; Anthropic's Claude Code sandbox
   routes all egress through a UDS to a proxy outside the sandbox with domain allowlists.
   The egress proxy is where the sandbox/harness boundary is enforced in practice.
7. **Image/dependency delivery.** Re-installing packages every session is the real
   cold-start cost. Fixes: snapshots (E2B, Vercel), templates, Vercel's container-registry
   custom images, Modal's lazy hydration — OverlayFS over a FUSE lazy-loading file server,
   turning torch's ~26,000 import syscalls into roughly one file load.

## 2. Why Kata-style container-runtime-integrated VMs miss these

Kata Containers (OpenInfra; merged from Intel Clear Containers and Hyper runV, 2017) is a
**container runtime** — an OCI/CRI-compatible containerd shim that wraps each pod in a VM
(QEMU default; Cloud Hypervisor and Firecracker optional). Its design center is steady-state
Kubernetes microservices: a Kata pod "looks like a standard container" and is expected to
stay up.

- **Per-pod memory ~15× a container.** Measured (TU Delft MSc thesis, 100 pods, 60 GiB/8-core
  worker, Kata 3.1.3, 2023): average pod footprint runc **10 MiB**, kata-qemu **152 MiB**,
  kata-firecracker **135 MiB**; Kata also reserves memory beyond the pod process. A
  1000-replica scale-out capped at 109 Running pods on the 128 GiB worker. At hundreds of MiB
  per idle sandbox, hosting sparse agent sessions is uneconomical.
- **Pod startup ~1 s+.** The microVM boots in 150–300 ms, but full pod startup through
  containerd-shim → QEMU → guest agent → CNI is ~1 s+ (Northflank production
  Kata-on-Cloud-Hypervisor: 1–2 s). Kata-with-Firecracker is forced onto the devmapper
  storage driver, making batch startup *worse* than QEMU-Kata (38 s vs 28 s per 100).
- **No snapshot-to-disk story.** No first-class pause/snapshot/restore API — the underlying
  Firecracker has it, but the CRI layer does not expose it as a pod operation. Every pause in
  Kata-land is a full pod delete/recreate.
- **No pay-for-idle.** A Running pod holds its QEMU process, guest RAM, and virtiofsd for as
  long as it runs; no model where the VM evaporates to a snapshot while the session
  logically persists.
- **Wrong control plane.** Kubernetes Deployments/Services/CNI/CSI are built for long-lived
  services; an agent-sandbox backend needs an SDK with `pause()/connect()/fork()` and
  per-second metering.

Nuance: Kata's *isolation* is fine — the mismatch is the lifecycle model and per-pod
overhead, not security.

## 3. Per-provider table

| Provider | Substrate | Key numbers | Constraint addressed |
| -------- | --------- | ----------- | -------------------- |
| **E2B** (open source, Apache-2.0 runtime) | Firecracker microVM per sandbox, orchestrator per node | resume ~1 s; pause ~4 s/GiB; paused = unbilled, kept indefinitely; 24 h continuous-run cap (Pro) | full-state pause/resume; hibernate between turns |
| **Vercel Sandboxes** (powers v0 / code agent) | Firecracker, dedicated kernel, persistent-by-default | FS auto-snapshot on stop; Active CPU $0.128/h + $0.0212/GB-h; 10,000 concurrent (Pro) | resume-from-snapshot skips dependency install; per-sandbox netns + firewall |
| **Modal** | gVisor (runsc) by default, VM runtime optional; container + memory snapshots; lazy hydration | snapshot restore ~2.5× faster than cold; torch import 5 s → 1.05 s; mem snapshots expire 7 d | cold-start elimination; GPU workloads (GPU state not snapshotted) |
| **Daytona** (open-source sandbox standard + cloud) | Linux containers default; separate VM class (Linux/Windows) | <90 ms claimed; pause/fork/memory snapshots VM-class only; container class = FS snapshot only | choice of isolation class per use case; fork of running VMs |
| **Fly.io Sprites** | Firecracker, own kernel | warm wake 100–500 ms; billing stops ~30 s after activity; CoW FS checkpoints | warm-but-unbilled idle |
| **Anthropic** | Tiered: gVisor + seccomp (claude.ai code execution); bubblewrap/Seatbelt + UDS egress proxy (Claude Code CLI); full VM (Cowork) | sandbox reduced permission prompts 84 %; ~93 % of prompts approved pre-sandbox; credentials kept outside the VM | proportional isolation; egress allowlisting as the real boundary |

## 4. Common substrate

- **Firecracker microVM** (AWS, Apache-2.0, built for Lambda/Fargate): KVM VMM, minimal
  device model, ~125 ms boot to userspace and <5 MiB overhead per instance (figures from the
  original Agache et al. 2020 paper — pre-2020 hardware; the current README no longer states
  them). Built-in snapshot/restore, demand fault paging, CPU oversubscription, per-thread
  seccomp, Jailer. Substrate of E2B, Vercel, Sprites.
- **gVisor**: userspace kernel intercepting syscalls — cheap to snapshot/restore, no guest
  kernel per sandbox, but syscall interception costs: Ant Group production measurements
  showed >10× native syscall cost (KVM platform); typical I/O-heavy workloads 10–30 % slower
  than native. Density and snapshot-ability vs steady-state throughput.
- **Cloud Hypervisor vs QEMU** (rust-vmm): CH boots ~200 ms vs Firecracker's ~125 ms,
  supports CPU/memory hotplug; QEMU carries the most memory overhead, broadest
  compatibility.
- **Snapshot/restore mechanics**: full-VM snapshots (memory + devices — E2B, Sprites);
  process-level runsc restore with background page-in (Modal, pages files 100 MiB–10 GiB);
  overlay/CoW filesystem snapshots (Vercel, Sprites); KSM/page-cache sharing for density
  (Kata ecosystem — no hard number verified this pass).
- **Warm pools**: the pool-free equivalent is Fly's warm-but-unbilled state.

## 5. Where this repo's measurements bear on it

Our own experiment quantifies the tradeoff the commercial providers hand-wave:

- **Density mechanism is page sharing** (`moca-side-by-side-report.md`): every Firecracker VM
  restored from a template's golden snapshot maps the same memfile — ~tens of MB effective
  per sandbox vs ~370 MB unshared QEMU (~10× at load50; 2.7 GB for ~50 sandboxes + standby
  pool vs 7.4 GB for 20 QEMU VMs).
- **The duty-cycle crossover, D\* ≈ 4.7 %** (load80 ladder appendix): fresh-VM-per-exec burns
  *less total CPU* than resident VMs below ~5 % duty cycle, and less memory at every duty
  cycle. The industry's 10 %-duty pricing assumption sits just above our measured crossover —
  we measured roughly where their business model breaks even.
- **The price of statelessness**: ~4 core-s of cold-rootfs compute per tool step (~96 % of
  stutter cost, only ~0.1–0.2 core-s of start/stop machinery); +13-point firecracker
  virtio-mmio tax vs QEMU (the VMM, not the kernel — kernel-matched experiment); collapse
  past the saturation knee at load80 (+500 % stretch).
- **Counterpoint to the warm-snapshot story**: firecracker snapshot/restore on our stack does
  **not** preserve the guest's file page cache (tmpfs survives, clean file-backed pages do
  not serve reads after restore) — directly relevant to anyone claiming snapshot-baked warmth
  (the E2B/Modal resume-latency numbers above are vendor-measured on their stacks, not
  verified here).

## 6. Uncertainty flags

- Firecracker "125 ms / <5 MiB" figures trace to the 2020 paper era, not restated in the
  current README.
- Kata boot "150–300 ms" is microVM boot; *pod* startup is ~1 s+ — say "pod startup ~1 s+"
  on a slide, never "125 ms".
- Daytona's <90 ms is a vendor claim with no published p50/p99 methodology; none of the four
  major providers publishes start-latency percentiles.
- Anthropic's Claude Code web sandbox = gVisor per the official "How we contain Claude"
  article (better source than older speculation).
- KSM page-cache-sharing numbers: not verified.
- Kata per-pod overheads come from one 2023 MSc thesis (Kata 3.1.3); treat as order-of-
  magnitude.

## Sources

- https://atlarge-research.com/pdfs/2023-asklavos-msc_thesis.pdf (Kata pod overheads)
- https://northflank.com/blog/what-are-kata-containers
- https://northflank.com/blog/guide-to-cloud-hypervisor
- https://northflank.com/blog/kata-containers-vs-gvisor
- https://github.com/firecracker-microvm/firecracker
- https://docs.e2b.dev/sandbox/persistence
- https://vercel.com/docs/sandbox/concepts · https://vercel.com/docs/sandbox/pricing
- https://modal.com/blog/mem-snapshots
- https://daytona.io/docs/en/sandboxes/
- https://fly.io/learn/sprites-vs-e2b-vs-modal-vs-daytona/
- https://www.anthropic.com/research/claude-code-sandboxing
- https://www.anthropic.com/research/how-we-contain-claude
- https://gvisor.dev/blog/2021/12/02/running-gvisor-in-production-at-scale-in-ant/
- https://gvisor.dev/blog/2023/04/28/systrap-release/

Local cross-references: [`../reports/moca-side-by-side-report.md`](../reports/moca-side-by-side-report.md)
(page sharing, cold-rootfs cost, duty-cycle crossover, warm-snapshot dead end).

---

*Assisted-By: Claude Code*
