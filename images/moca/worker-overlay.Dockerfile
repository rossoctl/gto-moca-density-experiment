# worker-overlay.Dockerfile -- turn a trajectories testbed image into a MOCA
# sandbox image (container tier, or the rootfs source for a microVM snapshot).
#
# Layers the remote-worker binary and rg on top of the testbed, checks MOCA's
# required tools (deploy/microvm/build-rootfs.sh's REQUIRED_TOOLS list) fail-closed
# at build time, and prepares /workspace as the per-run mount point.
#
# Two deliberate differences from remote-worker/Dockerfile:
#
# Git identity: NOT an unconditional /etc/gitconfig. The testbed sets
# `user.useConfigOnly true` so an agent commit without its own identity FAILS
# LOUDLY instead of silently committing under a system identity
# (testbed.Dockerfile step 3); a system [user] floor would defeat that on
# purpose. But moca's build-snapshot.sh probes guest git identity with a
# throwaway repo under /tmp and refuses to snapshot without one. Resolution: a
# conditional include scoped to /tmp -- scratch repos get the default identity,
# the probe passes, and /testbed plus its per-run /workspace copy stay
# identity-free so replayed trajectories fail commits exactly as loudly as
# they did in the container-tier runs.
#
# /workspace: NOT a symlink to /testbed (an earlier container-tier draft did
# that). In the microVM tier the guest agent mounts the per-run ext4 workspace
# image AT /workspace and runs every command from there (cmd/guest-agent:
# workdir flag, default /workspace); build-snapshot.sh also mkdir -p's it as a
# mount point, which a symlink breaks. The base rootfs -- including /testbed
# -- is the SHARED snapshot layer, so the pristine testbed travels in the
# rootfs as a SEED: the trajectory driver's first exec copies /testbed into
# the per-run /workspace, and the agent edits that private copy.

# Build with the moca/ directory as the context (bin/ holds the two binaries):
#
#   podman build -f moca/worker-overlay.Dockerfile \
#     --build-arg TESTBED_IMAGE=icr.io/vopo/swebench-django__django-10914:latest \
#     -t icr.io/vopo/moca-sbx-sweb-django-10914:latest moca/
#
# bin/remote-worker: built from the moca repo, CGO_ENABLED=0 go build ./cmd/worker.
# bin/rg: ripgrep 15.2.0 x86_64-unknown-linux-musl, the exact pin (and sha256) from
# remote-worker/Dockerfile's rg stage -- static, so it needs no libc from the testbed.

ARG TESTBED_IMAGE
FROM ${TESTBED_IMAGE}

USER root

# remote-worker (inert in a guest; build-rootfs.sh strips it before snapshotting)
# and rg (implementation of the Find/Glob and Grep tools, not a scheduling label
# -- see remote-worker/Dockerfile's rg stage for why it is pinned there).
COPY bin/remote-worker /usr/local/bin/remote-worker
COPY bin/rg /usr/local/bin/rg
# gh (GitHub CLI, pinned static release): moca's build-rootfs.sh requires it
# in the rootfs toolchain (advertised as a tool); the swebench bases lack it.
COPY bin/gh /usr/local/bin/gh

# swebench base images are Ubuntu; `file` is the one required tool they don't
# ship. base64/ls/cat come with coreutils, bash/git/python3 with the eval image.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends file; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    for t in bash git rg gh python3 base64 file ls cat; do command -v "$t" >/dev/null || { \
      echo "FATAL: required tool $t missing -- MOCA would advertise it and fail the first call" >&2; exit 1; }; done

# Slim for density: the sweb.eval images ship the conda package cache
# (/opt/miniconda3/pkgs -- often bigger than the env it installed), index
# tarballs and pip caches. None of it is needed to RUN the env, and the rootfs
# size is paid once per benchmark in snapshot restore time and page-cache
# pressure, so every MB off here is a MB off every restore. Guarded so a
# non-conda layout (shouldn't happen for sweb.eval) doesn't fail the build.
RUN set -eux; \
    if [ -x /opt/miniconda3/bin/conda ]; then /opt/miniconda3/bin/conda clean -y --all || true; fi; \
    rm -rf /opt/miniconda3/pkgs /root/.cache /home/*/.cache \
           /opt/miniconda3/envs/testbed/share/*.a 2>/dev/null || true; \
    find /usr/lib /opt/miniconda3 -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# safe.directory: the guest agent execs as root (HOME=/ on the read-only rootfs, so
# neither --global nor a per-user exception is reachable), while the seeded testbed
# tree is uid 1000 -- git refuses "dubious ownership" without this. Scoped to
# /workspace only; /testbed in the shared rootfs stays untrusted on purpose.
RUN printf '[includeIf "gitdir:/tmp/"]\n\tpath = /etc/autobench-git-identity\n' > /etc/gitconfig \
 && printf '[user]\n\tname = MOCA sandbox\n\temail = sandbox@moca.invalid\n' > /etc/autobench-git-identity \
 && printf '[safe]\n\tdirectory = /workspace\n' >> /etc/gitconfig

# /etc/hosts CANNOT be fixed here: the image's copy is empty (container
# runtimes inject it at run time) and buildah silently drops the path from
# every layer, RUN and COPY alike -- the build succeeds and the file is absent.
# build-snapshots.sh patches it into the extracted rootfs instead; see the
# comment there for the failure this prevents (getaddrinfo('localhost')).

# /workspace: the per-run workspace mount point (see header). Plain directory,
# writable by uid 1000. HOME for uid 1000 (which owns the /testbed seed).
RUN install -d -o 1000 -g 1000 -m 775 /workspace
RUN install -d -o 1000 -g 1000 -m 700 /home/sandbox
RUN install -d -o 1000 -g 1000 -m 700 /home/sandbox
ENV HOME=/home/sandbox
# The swebench harness runs everything under `conda activate testbed`; the image
# alone leaves PATH pointing at conda base (bare python, no numpy/xarray/...).
# Prepending the env's bin is the Docker equivalent, so every exec the worker
# runs gets the testbed interpreter without a per-command activation.
ENV PATH=/opt/miniconda3/envs/testbed/bin:${PATH}
WORKDIR /testbed
USER 1000
ENTRYPOINT ["/usr/local/bin/remote-worker"]
