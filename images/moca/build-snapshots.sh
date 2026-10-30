#!/bin/bash
# build-snapshots.sh -- the host side of template generation: for each instance,
# extract the rootfs from its moca-sbx-* image, patch what the image build
# cannot express, and build the Firecracker golden snapshot.
#
#   prepare_moca_images.sh  (images/, user)  -> container images
#   build-snapshots.sh      (this, root)     -> /srv/snapshots/<template-id>
#   install-host.sh         (moca repo, root)-> sbx-worker@<worker> units
#
# Must run as root (loop-free but privileged: podman in root's storage, /dev/kvm,
# cgroups, the jailer). Run via sudo from images/:
#
#   sudo ./moca/build-snapshots.sh [instance ...]     # default: all known
#   sudo KERNEL=/srv/vmlinux-6.18.56 ./moca/build-snapshots.sh
#
# The /etc/hosts patch is here because it CANNOT be in the Dockerfile: buildah
# manages /etc/hosts (and resolv.conf, hostname) itself and silently drops the
# path from every layer, RUN and COPY alike -- the build succeeds and the file
# is simply absent (verified empirically both ways). The microVM has no
# container runtime to inject one at boot, so without this patch
# getaddrinfo('localhost') fails and anything binding a server to localhost
# (Django's LiveServerTests) errors at setUp. This restores what the container
# and QEMU tiers always had.
set -euo pipefail

cd "$(dirname "$0")/.."
MOCADIR=${MOCADIR:-$HOME/repo/rossoctl/moca}
REGISTRY=${REGISTRY:-icr.io/vopo}
KERNEL=${KERNEL:-/srv/vmlinux-6.18.56}
GUEST_RAM_MB=${GUEST_RAM_MB:-2048}
ROOTFS_ROOT=${ROOTFS_ROOT:-/srv/rootfs}
SNAP_ROOT=${SNAP_ROOT:-/srv/snapshots}

# instance dir -> template id; keep in sync with prepare_moca_images.sh and
# templates-swebench.json.
declare -A TID=(
  [django__django-10914]=sweb-django-10914
  [django__django-11133]=sweb-django-11133
  [pydata__xarray-4094]=sweb-xarray-4094
)

instances=("$@")
[ ${#instances[@]} -gt 0 ] || instances=("${!TID[@]}")

[ "$(id -u)" = 0 ] || { echo "must run as root (root podman, /dev/kvm, cgroups)" >&2; exit 1; }
[ -r "$KERNEL" ] || { echo "KERNEL=$KERNEL not readable (Firecracker CI kernel; see moca's deploy/microvm/P4-ON-P6.md for the fetch recipe)" >&2; exit 1; }
[ -d "$MOCADIR/remote-worker" ] || { echo "MOCADIR=$MOCADIR has no remote-worker/" >&2; exit 1; }
[ -r moca/hosts ] || { echo "moca/hosts (the /etc/hosts patch) is missing" >&2; exit 1; }

for image in "${instances[@]}"; do
  tid=${TID[$image]:-$(echo "sweb-$image" | tr '_' '-')}
  echo "=== $tid: rootfs"
  rm -rf "$ROOTFS_ROOT/$tid"
  "$MOCADIR/deploy/microvm/build-rootfs.sh" --image "$REGISTRY/moca-sbx-$tid:latest" --out "$ROOTFS_ROOT/$tid"

  echo "=== $tid: patching /etc/hosts (buildah drops it from image layers)"
  cat moca/hosts > "$ROOTFS_ROOT/$tid/etc/hosts"

  echo "=== $tid: snapshot"
  rm -rf "$SNAP_ROOT/$tid"
  "$MOCADIR/deploy/microvm/build-snapshot.sh" \
    --kernel "$KERNEL" --rootfs "$ROOTFS_ROOT/$tid" \
    --agent "$MOCADIR/remote-worker" --image "$tid" \
    --guest-ram-mb "$GUEST_RAM_MB" --out "$SNAP_ROOT/$tid"
  echo "=== $tid: DONE -> $SNAP_ROOT/$tid"
done
