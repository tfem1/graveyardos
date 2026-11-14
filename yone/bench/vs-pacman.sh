#!/usr/bin/env bash
# Yone against pacman, on Arch Linux (run as root in an archlinux container):
#
#   yone/bench/vs-pacman.sh <yone binary> [package ...]
#
# The same Arch packages are served to both from a local repository: for
# pacman as they are (repo-add), for Yone repacked (same files, zstd, signed).
# Each manager installs them into an empty root in one transaction, without
# dependencies, scriptlets or hooks (the roots have no shell); both copy the
# packages into their cache, check them and extract them. Each run starts
# from a cold package cache and an empty root; the median of 5 runs is kept.
set -euo pipefail
yone=$(realpath "${1:?yone binary}"); shift
[ $# -gt 0 ] || set -- llvm-libs python qt6-base gcc
pkgs=("$@")
W=${BENCH_DIR:-/var/tmp/yone-bench}
rm -rf "$W"; mkdir -p "$W"/{arch,yone,recipes,keys}
cd "$W"

echo "==> downloading ${pkgs[*]}"
pacman -Sw --noconfirm --cachedir "$W/arch" "${pkgs[@]}" > /dev/null
find "$W/arch" -name '*.sig' -delete
repo-add -q "$W/arch/bench.db.tar.gz" "$W"/arch/*.pkg.tar.zst
cat > "$W/pacman.conf" << CONF
[options]
Architecture = auto
SigLevel = Never
LocalFileSigLevel = Never
[bench]
Server = file://$W/arch
CONF

echo "==> repacking for Yone"
"$yone" keygen "$W/keys/k" > /dev/null
for f in "$W"/arch/*.pkg.tar.zst; do
  n=$(bsdtar -xOf "$f" .PKGINFO | sed -n 's/^pkgname = //p')
  v=$(bsdtar -xOf "$f" .PKGINFO | sed -n 's/^pkgver = //p'); v=${v#*:}
  mkdir -p "recipes/$n"
  cat > "recipes/$n/YONEBUILD" << R
name = $n
version = ${v%-*}
release = ${v##*-}
desc = $n, from Arch Linux

@package
bsdtar -xpf $f -C "\$PKGDIR"
rm -f "\$PKGDIR"/.PKGINFO "\$PKGDIR"/.MTREE "\$PKGDIR"/.BUILDINFO "\$PKGDIR"/.INSTALL
R
  "$yone" build "recipes/$n" --out "$W/yone" --sign "$W/keys/k.key" --compress zstd --color never > /dev/null
done
"$yone" repo-index "$W/yone" --name bench --sign "$W/keys/k.key" --color never > /dev/null
names=$(cd recipes && echo *)
echo "    packages: $names"
du -sh "$W/arch"/*.pkg.tar.zst "$W/yone"/*.yon | sed 's|'"$W"'/||' | sed 's/^/    /'

pac_run() {
  rm -rf "$W/proot"; mkdir -p "$W/proot/var/lib/pacman" "$W/pcache" "$W/nohooks"
  rm -rf "$W/pcache"/*
  pacman -r "$W/proot" --config "$W/pacman.conf" --cachedir "$W/pcache" --hookdir "$W/nohooks" -Sy > /dev/null
  sync; local t0=$EPOCHREALTIME
  pacman -r "$W/proot" --config "$W/pacman.conf" --cachedir "$W/pcache" --hookdir "$W/nohooks" \
    -S --noconfirm -dd --noscriptlet $names > "$W/pacman.log" 2>&1
  echo "$EPOCHREALTIME - $t0" | bc
}
yone_run() {
  rm -rf "$W/yroot"; mkdir -p "$W/yroot/etc/yone/keys"
  cp "$W/keys/k.pub.pem" "$W/yroot/etc/yone/keys/"
  printf '[options]\nsiglevel = required\n\n[bench]\nserver = file://%s/yone\n' "$W" > "$W/yroot/etc/yone/yone.conf"
  "$yone" --root "$W/yroot" --noconfirm --color never refresh > /dev/null
  sync; local t0=$EPOCHREALTIME
  "$yone" --root "$W/yroot" --noconfirm --color never install $names > "$W/yone.log" 2>&1
  echo "$EPOCHREALTIME - $t0" | bc
}
median() { sort -n | sed -n 3p; }

echo "==> timing (5 runs each, alternated)"
: > p.txt; : > y.txt
for i in 1 2 3 4 5; do
  pac_run >> p.txt; yone_run >> y.txt
done
p=$(median < p.txt) y=$(median < y.txt)
files_p=$(find "$W/proot" -not -path '*/var/lib/pacman*' -not -path '*/var/cache*' | wc -l)
files_y=$(find "$W/yroot" -not -path '*/var/lib/yone*' -not -path '*/var/cache*' -not -path '*/etc/yone*' | wc -l)
echo "    pacman runs: $(tr '\n' ' ' < p.txt)"
echo "    yone runs:   $(tr '\n' ' ' < y.txt)"
echo
printf 'pacman %s: %.2f s\n' "$(pacman -Q pacman | cut -d' ' -f2)" "$p"
printf 'yone:          %.2f s   (%.2fx pacman)\n' "$y" "$(echo "$p / $y" | bc -l)"
echo "entries installed: pacman $files_p, yone $files_y"
