# tb-worker-overlay.Dockerfile -- turn a tb-microvm image (images/tb30/<task>/
# microvm.Dockerfile) into a MOCA sandbox image for Terminal-Bench tasks.
#
# Peer of worker-overlay.Dockerfile (which see for the SWE-bench flavour and
# the rationale of every step this file abbreviates). Differences forced by
# the TB shape:
#   - the tb-microvm base already moved /app to /opt/testbed-src and symlinked
#     /app -> /workspace, and already installed git/file/python3 where the
#     environment lacked them -- no apt step, no conda, no /testbed;
#   - TB trajectories run as root (the environment images' own user), so the
#     workspace stays root-owned and there is no uid-1000 home;
#   - WORKDIR /workspace (== /app via the symlink).
#
# Build with the moca/ directory as the context (bin/ holds the binaries):
#
#   podman build -f moca/tb-worker-overlay.Dockerfile \
#     --build-arg TESTBED_IMAGE=icr.io/vopo/moca-sbx-tb-fix-git:latest \
#     -t icr.io/vopo/moca-sbx-tb-fix-git:latest moca/

ARG TESTBED_IMAGE
FROM ${TESTBED_IMAGE}

USER root

# remote-worker (inert in a guest; build-rootfs.sh strips it before
# snapshotting), rg and gh: moca's build-rootfs.sh requires all three in the
# rootfs toolchain (REQUIRED_TOOLS); the tb-microvm import deliberately ships
# none of them (this repo's run environment provides its own moca runners).
COPY bin/remote-worker /usr/local/bin/remote-worker
COPY bin/rg /usr/local/bin/rg
COPY bin/gh /usr/local/bin/gh

RUN set -eux; \
    for t in bash git rg gh python3 base64 file ls cat; do command -v "$t" >/dev/null || { \
      echo "FATAL: required tool $t missing -- MOCA would advertise it and fail the first call" >&2; exit 1; }; done

# Git identity, same resolution as worker-overlay.Dockerfile: the guest agent
# execs as root (HOME=/ on the read-only rootfs), build-snapshot.sh probes
# guest git identity with a throwaway repo under /tmp and refuses without
# one. Scoped to /tmp so replayed trajectories fail identity-free commits as
# loudly as they did in the container tier; /workspace is safe.directory
# because the seeded tree is used by root while image layers may be uid 1000.
RUN printf '[includeIf "gitdir:/tmp/"]\n\tpath = /etc/autobench-git-identity\n' > /etc/gitconfig \
 && printf '[user]\n\tname = MOCA sandbox\n\temail = sandbox@moca.invalid\n' > /etc/autobench-git-identity \
 && printf '[safe]\n\tdirectory = /workspace\n' >> /etc/gitconfig

WORKDIR /workspace
USER root
ENTRYPOINT ["/usr/local/bin/remote-worker"]
