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
[[ -f $Vmlinuz ]] || { echo "no kernel $Vmlinuz" >&2; exit 1; }
[[ -d /lib/modules/$KERNEL_VERSION ]] || { echo "no modules for $KERNEL_VERSION" >&2; exit 1; }

# Initrd: the guest needs a GENERIC initrd -- root=/dev/vda, virtio+ext4 only.
# Host distros diverge here and the wrong one hangs or panics the guest:
#   - initramfs-tools (Ubuntu <= 24.04): /boot/initrd.img-* is generic enough,
#     honors the root= kernel arg as-is.
#   - dracut (Ubuntu >= 26.04): /boot/initrd.img-* is built HOSTONLY -- it
#     embeds the build host's root-disk UUID and waits forever for it
#     (dracut-initqueue timeout), and its size (~110MB) can deadlock a 1GiB
#     guest at unpack. So with dracut we build a slim no-hostonly initrd
#     (base rootfs-block fs-lib + virtio/ext4 drivers, ~36MB), which boots
#     to the shell in ~2s at VM_MEM_MB=1024.
# INITRD_IMG env var overrides (pre-built or to reuse a cached one).
if [[ -n "${INITRD_IMG:-}" ]]; then
    Initrd=$INITRD_IMG
    [[ -f $Initrd ]] || { echo "no initrd $Initrd (INITRD_IMG)" >&2; exit 1; }
elif command -v dracut >/dev/null 2>&1; then
    Initrd=/tmp/qcow2-initrd-"$KERNEL_VERSION".img
    if [[ ! -f $Initrd ]]; then
        echo "=== building slim generic initrd for $KERNEL_VERSION (dracut host)"
        dracut --no-hostonly --force --kver "$KERNEL_VERSION" \
              -m "base rootfs-block fs-lib shutdown" \
              --drivers "virtio virtio_blk virtio_pci virtio_ring ext4" \
              "$Initrd"
    fi
else
    Initrd=/boot/initrd.img-"$KERNEL_VERSION"
    [[ -f $Initrd ]] || { echo "no initrd $Initrd (install linux-image-$KERNEL_VERSION)" >&2; exit 1; }
fi

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
    sudo chown -R "$OWNER":$(id -gn "$OWNER" 2>/dev/null || echo "$OWNER") "$out"

    echo "=== $name: saving 'ready' memory snapshot (boots once under KVM)"
    python3 -I "$HERE/../replay/replay.py" vm-snapshot "$out" \
        || { echo "snapshot failed for $name"; exit 1; }
    # state.bin is written by qemu (root under sudo) AFTER the chown above;
    # the runs resume as the unprivileged user via `cat state.bin`, so hand
    # it back too or every vm-start fails with permission denied.
    sudo chown "$OWNER":$(id -gn "$OWNER" 2>/dev/null || echo "$OWNER") "$out/state.bin" 2>/dev/null || true

    echo "=== $name: done -> $out"
done
