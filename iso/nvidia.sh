#!/usr/bin/env bash
# The NVIDIA layer of a GraveyardOS image: $LFS/.graveyard/nvidia.sfs, put on
# the ISO by iso/build.sh with a boot entry of its own.
#
#   sudo iso/nvidia.sh <arch-build work directory>      after iso/desktop.sh
#
# What is in it: NVIDIA's user space and GSP firmware (Arch's nvidia-utils,
# built by arch-build, installed with Yone) and NVIDIA's open kernel modules
# of the same version, built here for GraveyardOS's kernel; nouveau
# blacklisted, kernel modesetting on. It is made as an overlay over the
# system, so it holds exactly what the driver adds or changes (Yone's
# database included): the live system stacks it with graveyard.nvidia=1, the
# installer copies it over the installed system when NVIDIA is chosen.
#
# The modules are not signed: the kernel accepts unsigned modules (no
# CONFIG_MODULE_SIG_FORCE), not under Secure Boot's lockdown.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
LFS=${LFS:-/mnt/lfs}
work=$(realpath "${1:?arch-build work directory}")
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -x "$LFS/usr/bin/yone" ] || { echo "no Yone in $LFS (iso/desktop.sh first)" >&2; exit 1; }
log() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
kver=$(cat "$LFS/.graveyard/kernel-release")
L=${GRAVEYARD_NVIDIA_WORK:-/mnt/nvidia-layer}
root=$L/root
rm -rf "$L"; mkdir -p "$L/upper" "$L/work" "$root"
mount -t overlay -o "lowerdir=$LFS,upperdir=$L/upper,workdir=$L/work" graveyard-nvidia "$root"
cleanup() {
  for m in var/lib/graveyard/desktop-repo dev/pts dev proc sys run; do
    umount "$root/$m" 2> /dev/null || true
  done
  umount "$root" 2> /dev/null || true
}
trap cleanup EXIT
mount --bind /dev "$root/dev"; mount -t devpts devpts "$root/dev/pts"
mount -t proc proc "$root/proc"; mount -t sysfs sysfs "$root/sys"; mount -t tmpfs tmpfs "$root/run"
inroot() { chroot "$root" /usr/bin/env -i HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin "$@"; }

log "NVIDIA's user space (nvidia-utils)"
install -d "$root/var/lib/graveyard/desktop-repo"
mount --bind "$work/repo" "$root/var/lib/graveyard/desktop-repo"
conf=$(cat "$root/etc/yone/yone.conf")
sed -i 's|^# server = .*|server = file:///var/lib/graveyard/desktop-repo|' "$root/etc/yone/yone.conf"
inroot yone --noconfirm --color never refresh > /dev/null
inroot yone --noconfirm --color never install nvidia-utils
ver=$(inroot yone --color never info nvidia-utils | sed -n 's/^Version *: *//p' | sed 's/-[^-]*$//')
[ -n "$ver" ] || { echo "cannot read the version of nvidia-utils" >&2; exit 1; }
printf '%s\n' "$conf" > "$root/etc/yone/yone.conf"
echo "   nvidia-utils $ver"

log "NVIDIA's open kernel modules $ver for Linux $kver"
# the kernel's source, prepared with the system's own configuration
ktar=$(grep -oE 'linux-[0-9.]+\.tar\.xz$' "$REPO/lfs/wget-list" | head -n 1)
kurl=$(grep -E "/$ktar\$" "$REPO/lfs/wget-list" | head -n 1)
ksum=$(awk -v f="$ktar" '$2 == f { print $1 }' "$REPO/lfs/sources.sha256")
src=$root/tmp/nvidia-build; mkdir -p "$src"
if [ -s "$LFS/sources/$ktar" ]; then cp "$LFS/sources/$ktar" "$src/"; else curl -fsSL --retry 3 -o "$src/$ktar" "$kurl"; fi
[ -z "$ksum" ] || echo "$ksum  $src/$ktar" | sha256sum -c --quiet
curl -fsSL --retry 3 -o "$src/open-gpu-kernel-modules.tar.gz" \
  "https://github.com/NVIDIA/open-gpu-kernel-modules/archive/refs/tags/$ver.tar.gz"
inroot sh -ec "
  cd /tmp/nvidia-build
  tar -xf $ktar && tar -xf open-gpu-kernel-modules.tar.gz
  cd ${ktar%.tar.xz}
  cp /boot/config-$kver .config
  cp /usr/lib/modules/$kver/build/Module.symvers .
  make -s olddefconfig
  make -s modules_prepare
  [ \"\$(make -s kernelrelease)\" = $kver ] || { echo 'the prepared kernel is not $kver' >&2; exit 1; }
  cd ../open-gpu-kernel-modules-$ver
  make -s -j\$(nproc) modules SYSSRC=/tmp/nvidia-build/${ktar%.tar.xz} SYSOUT=/tmp/nvidia-build/${ktar%.tar.xz}
  install -d /usr/lib/modules/$kver/extramodules
  install -m 644 kernel-open/*.ko /usr/lib/modules/$kver/extramodules/
  depmod $kver
"
rm -rf "$src"
ls "$root/usr/lib/modules/$kver/extramodules/" | sed 's/^/   /'

log "configuration"
install -d "$root/etc/modprobe.d"
cat > "$root/etc/modprobe.d/graveyard-nvidia.conf" << 'EOF'
# GraveyardOS NVIDIA layer: NVIDIA's driver instead of nouveau
blacklist nouveau
options nvidia-drm modeset=1 fbdev=1
options nvidia NVreg_PreserveVideoMemoryAllocations=1
EOF
for unit in nvidia-suspend nvidia-hibernate nvidia-resume; do
  [ -e "$root/usr/lib/systemd/system/$unit.service" ] && inroot systemctl enable "$unit" > /dev/null 2>&1 || true
done
rm -rf "$root/var/cache/yone/pkg" "$root/tmp/"*

cleanup
trap - EXIT
# Overlay bookkeeping is not part of the layer: whiteouts (nothing the
# driver needs removes a file of the system) and the mount point above.
n=$(find "$L/upper" -type c | wc -l)
[ "$n" -eq 0 ] || { echo "   $n whiteout(s) dropped:"; find "$L/upper" -type c | sed "s|^$L/upper|     |" | head; find "$L/upper" -type c -delete; }
rmdir "$L/upper/var/lib/graveyard/desktop-repo" 2> /dev/null || true

log "nvidia.sfs"
mksquashfs "$L/upper" "$LFS/.graveyard/nvidia.sfs" -noappend -no-progress -comp zstd \
  -Xcompression-level "${GRAVEYARD_SFS_LEVEL:-19}" -b 1M > "$L/mksquashfs.log"
printf '   %d MiB, NVIDIA %s, Linux %s\n' "$(($(stat -c %s "$LFS/.graveyard/nvidia.sfs") / 1048576))" "$ver" "$kver"
rm -rf "$L"
