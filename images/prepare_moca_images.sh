#!/bin/bash
# prepare_moca_images.sh -- peer driver to prepare_ctr_images.sh / prepare_qcow2_images.sh:
# turns each testbed.Dockerfile into a MOCA sandbox image (moca-sbx-<template-id>).
#
#   prepare_ctr_images.sh   -> container testbed images       (swebench-<instance>)
#   prepare_qcow2_images.sh -> direct-kernel-boot qcow2 VMs   (<instance>.qcow2)
#   prepare_moca_images.sh  -> MOCA sandbox images            (moca-sbx-<template-id>)
#
# The MOCA-specific assets live in moca/ (worker-overlay.Dockerfile, templates
# JSON). This driver owns the moca-side inputs that are NOT in this repo:
#   - the remote-worker binary, built from a moca checkout (MOCA_REF pin below);
#   - ripgrep, the pinned static musl build from remote-worker/Dockerfile.
# The host-side pipeline after this (build-rootfs.sh / build-snapshot.sh /
# install-host.sh) is NOT copied in -- it is run from the pinned moca checkout
# so the runtime and this repo can't drift apart. Record MOCA_COMMIT with every
# measurement result, like the testbed pins its image digest.
#
# Usage (from anywhere):
#   ./prepare_moca_images.sh [instance ...]      # default: all known instances
#   MOCADIR=~/repo/rossoctl/moca ./prepare_moca_images.sh
#   SKIP_BINARY_BUILD=1 ./prepare_moca_images.sh # reuse moca/bin/ if present
set -euo pipefail

cd "$(dirname "$0")"
REGISTRY=${REGISTRY:-icr.io/vopo}
MOCADIR=${MOCADIR:-$HOME/repo/rossoctl/moca}

# The moca commit this build was last validated against. Not enforced (a dirty
# checkout is legitimate while iterating); stamped into the image labels and
# echoed so results can record it.
MOCA_REF_DEFAULT=main
MOCA_COMMIT=$(git -C "$MOCADIR" rev-parse HEAD 2>/dev/null || echo "unknown-$MOCA_REF_DEFAULT")

# ripgrep pin: MUST match remote-worker/Dockerfile's rg stage (version + x86_64
# sha256) -- the Glob/Grep tool semantics were characterised on this build.
RG_VERSION=15.2.0
RG_SHA256_X86_64=33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c

# gh pin: moca main's build-rootfs.sh requires the GitHub CLI in the rootfs
# toolchain; the swebench bases don't ship it.
GH_VERSION=2.63.2
GH_SHA256_X86_64=912fdb1ca29cb005fb746fc5d2b787a289078923a29d0f9ec19a0b00272ded00

# instance dir -> (template id, worker id). Template ids must match
# ^[a-z0-9][a-z0-9-]{0,30}$ -- underscores are not allowed, hence the rename.
# Keep in sync with moca/templates-swebench.json.
declare -A TID=(
  [django__django-10914]=sweb-django-10914
  [django__django-11133]=sweb-django-11133
  [pydata__xarray-4094]=sweb-xarray-4094
)

mkdir -p moca/bin

if [ -z "${SKIP_BINARY_BUILD:-}" ] || [ ! -x moca/bin/remote-worker ]; then
  echo "=== building remote-worker from $MOCADIR (${MOCA_COMMIT})"
  [ -d "$MOCADIR/remote-worker" ] || { echo "MOCADIR=$MOCADIR has no remote-worker/ -- set MOCADIR to a moca checkout" >&2; exit 1; }
  (cd "$MOCADIR/remote-worker" && CGO_ENABLED=0 go build -trimpath -o "$OLDPWD/moca/bin/remote-worker" ./cmd/worker)
fi

if [ -z "${SKIP_BINARY_BUILD:-}" ] || [ ! -x moca/bin/rg ]; then
  echo "=== fetching ripgrep ${RG_VERSION} (pinned)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  tarball="ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl.tar.gz"
  curl -fsSL -o "$tmp/$tarball" \
    "https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/${tarball}"
  printf '%s  %s\n' "$RG_SHA256_X86_64" "$tmp/$tarball" | sha256sum -c -
  tar -xzf "$tmp/$tarball" -C "$tmp"
  install -m 755 "$tmp/ripgrep-${RG_VERSION}-x86_64-unknown-linux-musl/rg" moca/bin/rg
fi

if [ -z "${SKIP_BINARY_BUILD:-}" ] || [ ! -x moca/bin/gh ]; then
  echo "=== fetching gh ${GH_VERSION} (pinned)"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  tarball="gh_${GH_VERSION}_linux_amd64.tar.gz"
  curl -fsSL -o "$tmp/$tarball" \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${tarball}"
  printf '%s  %s\n' "$GH_SHA256_X86_64" "$tmp/$tarball" | sha256sum -c -
  tar -xzf "$tmp/$tarball" -C "$tmp"
  install -m 755 "$tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" moca/bin/gh
fi

instances=("$@")
[ ${#instances[@]} -gt 0 ] || instances=("${!TID[@]}")

for image in "${instances[@]}"; do
  tid=${TID[$image]:-$(echo "sweb-$image" | tr '_' '-')}
  testbed="$REGISTRY/swebench-$image:latest"
  echo "=== $image -> moca-sbx-$tid"

  # The testbed image is build_images.sh's output; build it here only if absent,
  # so the two drivers stay composable either way.
  podman image exists "$testbed" || \
    podman build -t "$testbed" -f "$image/testbed.Dockerfile" "$image"

  podman build -f moca/worker-overlay.Dockerfile \
    --build-arg TESTBED_IMAGE="$testbed" \
    --label "io.rossoctl.density.moca-commit=$MOCA_COMMIT" \
    --label "io.rossoctl.density.template-id=$tid" \
    -t "$REGISTRY/moca-sbx-$tid:latest" moca/

  echo "=== $image done -> $REGISTRY/moca-sbx-$tid:latest (moca $MOCA_COMMIT)"
done
