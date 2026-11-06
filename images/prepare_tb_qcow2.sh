#!/bin/bash
# prepare_tb_qcow2.sh -- the VM prep stage for Terminal-Bench tasks.
# Peer of prepare_qcow2_images.sh (which see for the SWE-bench flavour):
# same pipeline -- export rootfs, ext4 disk, host kernel triplet, 'ready'
# memory snapshot under KVM -- but the base image is the task's pinned
# environment image (icr.io/vopo/tb-env-<task>, retagged from the digest in
# images/tb30/<task>/environment.txt), not a testbed.Dockerfile build.
#
# Usage: sudo ./prepare_tb_qcow2.sh [task ...]   # default: all tb30 tasks
#   e.g. sudo ./prepare_tb_qcow2.sh fix-git regex-log
#
# Must run in a shell with KVM access:  sg kvm -c "sudo ./prepare_tb_qcow2.sh"
# Produces images/tb30/<task>/vm/ (<task>.qcow2, state.bin, vmlinuz-*, initrd-*)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# --- tunables ---------------------------------------------------------------
VIRTUAL_SIZE_GB=4             # TB environments are small (81-318 MB)
REGISTRY=icr.io/vopo
# ----------------------------------------------------------------------------

KERNEL_VERSION=""
if [[ -z "$KERNEL_VERSION" ]]; then
    KERNEL_VERSION=$(ls /boot/vmlinuz-* | sed 's|.*/vmlinuz-||' | sort -V | tail -1)
fi
Vmlinuz=/boot/vmlinuz-"$KERNEL_VERSION"
[[ -f $Vmlinuz ]] || { echo "no kernel $Vmlinuz" >&2; exit 1; }
[[ -d /lib/modules/$KERNEL_VERSION ]] || { echo "no modules for $KERNEL_VERSION" >&2; exit 1; }

# Same generic-initrd rule as prepare_qcow2_images.sh: dracut hosts (Ubuntu
# >= 26.04) ship a hostonly initrd that hangs the guest; build the slim one.
if [[ -n "${INITRD_IMG:-}" ]]; then
    Initrd=$INITRD_IMG
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
    [[ -f $Initrd ]] || { echo "no initrd $Initrd" >&2; exit 1; }
fi

OWNER=${SUDO_USER:-root}

tasks=("$@")
[ ${#tasks[@]} -gt 0 ] || tasks=( $(ls tb30 | grep -v '\.' ) )

for task in "${tasks[@]}"; do
    dir="$HERE/tb30/$task"
    [[ -d $dir ]] || { echo "no images/tb30/$task, skipping" >&2; continue; }
    name=$task
    image="$REGISTRY/tb-env-$name:latest"
    podman image exists "$image" \
        || { echo "no $image (pull the digest from tb30/$task/environment.txt and retag)" >&2; exit 1; }

    out=$dir/vm
    echo "=== $name: exporting rootfs from $image"
    mkdir -p "$out"
    rootfs_tar=$out/rootfs.tar
    ctr="totbvm-$name-$$"
    podman create --name "$ctr" "$image" > /dev/null
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
    cat > /tmp/fstab.$$ <<EOF
/dev/vda  /  ext4  defaults,noatime  0 1
EOF
    sudo cp /tmp/fstab.$$ "$out/mnt/etc/fstab"
    rm -f /tmp/fstab.$$
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

    echo "=== $name: saving 'ready' memory snapshot (boots once under KVM)"
    python3 -I "$HERE/../replay/replay.py" vm-snapshot "$out" \
        || { echo "snapshot failed for $name"; exit 1; }
    # state.bin is written by qemu (root under sudo) AFTER the chown above
    sudo chown "$OWNER":$(id -gn "$OWNER" 2>/dev/null || echo "$OWNER") \
        "$out/state.bin" 2>/dev/null || true
    sudo chown -R "$OWNER":$(id -gn "$OWNER" 2>/dev/null || echo "$OWNER") "$out"

    echo "=== $name: done -> $out"
done
