# worker-overlay.Dockerfile -- turn a trajectories testbed image into a MOCA
# sandbox image (container tier, or the rootfs source for a microVM snapshot).
#
# Layers the remote-worker binary and rg on top of the testbed, checks MOCA's
# required tools (deploy/microvm/build-rootfs.sh's REQUIRED_TOOLS list) fail-closed
# at build time, and lands the harness cwd (/workspace) inside the testbed.
#
# Deliberately NOT done here, unlike remote-worker/Dockerfile:
#  - no /etc/gitconfig. The testbed sets `user.useConfigOnly true` so an agent
#    commit without its own identity FAILS LOUDLY instead of silently committing
#    under a system identity (testbed.Dockerfile step 3). A system [user] floor
#    would defeat that on purpose.
#  - no new /workspace directory. /testbed is the instrument; /workspace is a
#    symlink to it so the harness cwd (KAGENTI_SANDBOX_CWD default /workspace)
#    and the autobench tree are the same place.

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

# swebench base images are Ubuntu; `file` is the one required tool they don't
# ship. base64/ls/cat come with coreutils, bash/git/python3 with the eval image.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends file; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    for t in bash git rg python3 base64 file ls cat; do command -v "$t" >/dev/null || { \
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

# Harness cwd -> testbed, HOME for uid 1000 (which owns /testbed), keep WORKDIR
# and USER pointed at the instrument the testbed.Dockerfile set up.
RUN ln -s /testbed /workspace
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
