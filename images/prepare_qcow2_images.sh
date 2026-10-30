#!/bin/bash
# to_qcow2.sh -- the VM prep stage: convert a testbed.Dockerfile into a
# resumable qcow2 image.
#
# Usage: sudo ./to_qcow2.sh <image-dir> [<image-dir> ...]
#   e.g. sudo ./to_qcow2.sh django__django-10914
#
# Must run in a shell with KVM access (snapshots are accelerator-pinned):
#   sg kvm -c "sudo ./to_qcow2.sh <image-dir>"
#
# Produces, in vm/<image-dir>/:
#   <image-dir>.qcow2      root filesystem disk (ext4 on the whole disk,
#                          root=/dev/vda); after prep it also carries the
#                          setup writes (mounted dirs' bookkeeping etc.)
#   state.bin              full VM state (RAM + devices) at 'ready': guest
#                          booted, console set up, /dev/shm + /run mounted.
#                          replay.py vm-start resumes from it sub-second
#                          (migration resume over a throwaway overlay) --
#                          the memory-file + base-image + diffs microVM model
#   vmlinuz-<version>      kernel from the build host, pinned next to the image
#   initrd-<version>       matching initramfs
#
# Kernel and modules come from the build host (Ubuntu 24.04). The guest's
# /lib/modules is populated from the same tree so the triplet is self-consistent.
# No network is configured inside the guest; talk to it over the serial console.
# The guest runs init=/bin/bash on console=ttyS0.

export IMAGELIST=${@:-django__django-10914  django__django-11133  pydata__xarray-4094}

HERE="$(cd "$(dirname "$0")" && pwd)"

set -euo pipefail

# --- tunables ---------------------------------------------------------------
VIRTUAL_SIZE_GB=10
KERNEL_VERSION=""            # empty = newest vmlinuz in /boot
REGISTRY=icr.io/vopo
# ----------------------------------------------------------------------------

#[[ $EUID -eq 0 ]] || { echo "must run as root (loop mounts)" >&2; exit 1; }
#[[ $# -ge 1 ]] || { echo "usage: $0 <image-dir> [<image-dir> ...]" >&2; exit 1; }

if [[ -z "$KERNEL_VERSION" ]]; then
    KERNEL_VERSION=$(ls /boot/vmlinuz-* | sed 's|.*/vmlinuz-||' | sort -V | tail -1)
fi
Vmlinuz=/boot/vmlinuz-"$KERNEL_VERSION"
Initrd=/boot/initrd.img-"$KERNEL_VERSION"
[[ -f $Vmlinuz ]] || { echo "no kernel $Vmlinuz" >&2; exit 1; }
[[ -f $Initrd ]] || { echo "no initrd $Initrd (install linux-image-$KERNEL_VERSION)" >&2; exit 1; }
[[ -d /lib/modules/$KERNEL_VERSION ]] || { echo "no modules for $KERNEL_VERSION" >&2; exit 1; }

# the unprivileged user to hand files back to (build host's galmasi)
OWNER=${SUDO_USER:-root}

for image in ${IMAGELIST}; do
    dir=$(realpath "$image")
    name=$(basename "$dir")
    dockerfile=$dir/testbed.Dockerfile
    [[ -f $dockerfile ]] || { echo "no $dockerfile, skipping" >&2; continue; }

    out=$dir/vm
    echo "=== $name: building container image"
    podman build -t "$REGISTRY/swebench-$name:latest" -f "$dockerfile" "$dir" > /dev/null

    echo "=== $name: exporting rootfs"
    mkdir -p "$out"
    rootfs_tar=$out/rootfs.tar
    # export needs a container, but not a running one: create, export, remove
    ctr="toqcow2-$name-$$"
    podman create --name "$ctr" "$REGISTRY/swebench-$name:latest" > /dev/null
    podman export "$ctr" -o "$rootfs_tar"
    podman rm "$ctr" > /dev/null
    
    echo "=== $name: building raw disk (${VIRTUAL_SIZE_GB}G)"
    raw=$out/$name.raw
    truncate -s "${VIRTUAL_SIZE_GB}G" "$raw"
    mkfs.ext4 -q -F "$raw"
    loop=$(sudo losetup --find --show "$raw")
    mkdir -p "$out/mnt"
    trap 'sudo umount "$out/mnt" 2>/dev/null; rmdir "$out/mnt" 2>/dev/null; sudo losetup -d "$loop" 2>/dev/null' EXIT
    sudo mount "$loop" "$out/mnt"
    sudo tar --numeric-owner -xpf "$rootfs_tar" -C "$out/mnt"

    echo "=== $name: injecting modules and fstab"
    sudo mkdir -p "$out/mnt/lib/modules"
    sudo cp -a "/lib/modules/$KERNEL_VERSION" "$out/mnt/lib/modules/"
    sudo mkdir -p "$out/mnt/etc"
    cat > /tmp/fstab <<EOF
/dev/vda  /  ext4  defaults,noatime  0 1
EOF
    sudo cp /tmp/fstab $out/mnt/etc/fstab
    sudo umount "$out/mnt"
    rmdir "$out/mnt"
    sudo losetup -d "$loop"
    trap - EXIT

    echo "=== $name: converting to qcow2"
    qemu-img convert -O qcow2 -c "$raw" "$out/$name.qcow2"
    rm -f "$raw" "$rootfs_tar"

    echo "=== $name: stashing kernel triplet"
    sudo cp "$Vmlinuz" "$out/vmlinuz-$KERNEL_VERSION"
    sudo cp "$Initrd" "$out/initrd-$KERNEL_VERSION"
    sudo chown -R $USER:$(id -gn "$USER") "$out"

    echo "=== $name: saving 'ready' memory snapshot (boots once under KVM)"
    python3 -I "$HERE/../replay/replay.py" vm-snapshot "$out" \
        || { echo "snapshot failed for $name"; exit 1; }

    echo "=== $name: done -> $out"
done
