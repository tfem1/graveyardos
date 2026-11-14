#!/usr/bin/env bash
# Build Arch-converted packages in the GraveyardOS base system, in order,
# until a time budget is spent: one pass of the arch-build workflow.
#
#   sudo arch/build-wave.sh <system> <work> <budget seconds>
#
#   system  the base system ($LFS of a graveyard-base run); packages are
#           installed into it as they are built, so the next ones find their
#           build dependencies (it is a throwaway copy)
#   work    the state carried from pass to pass: repo/ (signed .yon files and
#           index), archkey.*, done, failed; plus logs/ for this pass
#
# Each package base of arch/order is imported (arch/import.py), built with
# Yone in a chroot, then installed. What fails is tried again by the next
# pass (its build dependencies may be there by then). Exit status 0 when the
# budget ran out or nothing was left.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
sys=$(realpath "${1:?system}") work=${2:?work} budget=${3:?seconds}
yone=${YONE:?a static yone binary (YONE=...)}
mkdir -p "$work" && work=$(realpath "$work")
mkdir -p "$work/repo" "$work/logs" "$work/recipes" "$work/arch-repos" "$work/sources"
touch "$work/done" "$work/failed"
# a package that failed may build now that more is built: every pass tries
# again all that is not done
mv "$work/failed" "$work/failed.before"; : > "$work/failed"
: > "$work/logs/.keep"
start=$SECONDS
log() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

LFS=$sys bash "$REPO/lfs/build.sh" mount > /dev/null
trap 'LFS=$sys bash "$REPO/lfs/build.sh" umount > /dev/null 2>&1 || true' EXIT
install -m 755 "$yone" "$sys/usr/local/bin/yone"
inroot() { chroot "$sys" /usr/bin/env -i HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin:/usr/local/bin "$@"; }

# The development key of this chain (the repository is not a release).
if [ ! -s "$work/archkey.key" ]; then
  inroot /usr/local/bin/yone keygen /tmp/archkey > /dev/null
  cp "$sys/tmp/archkey.key" "$sys/tmp/archkey.pub.pem" "$work/"
fi
rm -rf "$sys/packages"; mkdir -p "$sys/packages/repo" "$sys/etc/yone/keys"
install -m 600 "$work/archkey.key" "$sys/packages/key.key"
cp "$work/archkey.pub.pem" "$sys/etc/yone/keys/arch-dev.pub.pem"
mount --bind "$work/repo" "$sys/packages/repo"
trap 'umount "$sys/packages/repo" 2> /dev/null || true; LFS=$sys bash "$REPO/lfs/build.sh" umount > /dev/null 2>&1 || true' EXIT
cat > "$sys/etc/yone/yone.conf" << 'EOF'
[options]
siglevel = required

[arch]
server = file:///packages/repo
EOF
index() { inroot /usr/local/bin/yone repo-index /packages/repo --name arch --sign /packages/key.key --color never > /dev/null; }

# What earlier passes built goes back into the system.
if ls "$work"/repo/*.yon > /dev/null 2>&1; then
  log "installing what earlier passes built"
  index
  inroot /usr/local/bin/yone --noconfirm --overwrite --color never refresh > /dev/null
  mapfile -t built < <(inroot /usr/local/bin/yone --color never list | sed 's|^[^/]*/||; s| .*||')
  inroot /usr/local/bin/yone --noconfirm --overwrite --color never install "${built[@]}" > "$work/logs/reinstall.log" 2>&1 \
    || { echo "reinstall failed:"; tail -n 20 "$work/logs/reinstall.log"; exit 1; }
fi

# build one recipe directory in the chroot and install what it made
build_dir() { # recipe dir (host path), log
  local src=$1 lf=$2 name d
  name=$(sed -n 's/^name = //p' "$src/YONEBUILD")
  d=$sys/packages/recipes/$name
  rm -rf "$d"; mkdir -p "$d/.yone-sources"; cp -a "$src/." "$d/"
  # Every source where yone looks first: the system being built has no curl.
  # Downloaded here (the importer already fetched Arch's), pinned on first use
  # when the recipe says "sha256 = TODO" (bootstrap).
  local line url f i=0
  while IFS= read -r line; do
    url=${line#source = }
    f=${url%%::*}; [ "$f" != "$url" ] || f=$(basename "$url")
    url=${url#*::}
    case $url in *://*) ;; *) continue ;; esac
    if [ -s "$work/sources/$f" ]; then cp "$work/sources/$f" "$d/.yone-sources/$f"
    else curl -fsSL --retry 3 -o "$d/.yone-sources/$f" "$url" >> "$lf" 2>&1 || true; cp "$d/.yone-sources/$f" "$work/sources/$f" 2> /dev/null || true
    fi
  done < <(grep '^source = ' "$d/YONEBUILD")
  while grep -q '^sha256 = TODO' "$d/YONEBUILD"; do
    i=$((i + 1))
    f=$(grep '^source = ' "$d/YONEBUILD" | sed -n "${i}p" | sed 's/^source = //'); f=${f%%::*}; f=$(basename "$f")
    sed -i "0,/^sha256 = TODO$/s//sha256 = $(sha256sum "$d/.yone-sources/$f" | cut -d' ' -f1)/" "$d/YONEBUILD"
  done
  timeout --kill-after=1m "${ARCH_PACKAGE_TIMEOUT:-150m}" setsid --wait chroot "$sys" /usr/bin/env -i \
    HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin:/usr/local/bin MAKEFLAGS="-j$(nproc)" \
    /usr/local/bin/yone build "/packages/recipes/$name" --out /packages/repo --sign /packages/key.key \
    --compress zstd --color never < /dev/null >> "$lf" 2>&1 || return 1
  index
  inroot /usr/local/bin/yone --noconfirm --overwrite --color never refresh >> "$lf" 2>&1 &&
    inroot /usr/local/bin/yone --noconfirm --overwrite --color never install "$name" >> "$lf" 2>&1
}

# The base system, declared to Yone: one package that provides every base
# package's name, so packages depending on glibc, perl... install.
if ! grep -qx graveyard-base-provides "$work/done"; then
  r=$work/recipes/graveyard-base-provides; mkdir -p "$r"
  {
    echo "name = graveyard-base-provides"; echo "version = 1"; echo "release = 1"
    echo "desc = The GraveyardOS base system, installed by the image itself"
    echo "provides = $(grep -v '^#' "$HERE/base-provides" | tr '\n' ' ' | tr -s ' ')"
    echo; echo "@package"; echo 'mkdir -p "$PKGDIR/usr/share/graveyard"; echo base > "$PKGDIR/usr/share/graveyard/base-provides"'
  } > "$r/YONEBUILD"
  build_dir "$r" "$work/logs/graveyard-base-provides.log" || { tail -n 20 "$work/logs/graveyard-base-provides.log"; exit 1; }
  echo graveyard-base-provides >> "$work/done"
fi

# Tools Arch builds with themselves, from arch/bootstrap first.
for r in "$HERE"/bootstrap/python-pyproject-hooks "$HERE"/bootstrap/python-installer "$HERE"/bootstrap/python-build "$HERE"/bootstrap/cmake; do
  b=$(basename "$r")
  grep -qx "$b" "$work/done" && continue
  printf '   %-30s ' "$b (bootstrap)"
  if build_dir "$r" "$work/logs/$b.log"; then
    sed -n 's/^# replaces: //p' "$r/YONEBUILD" | tr ' ' '\n' | grep . >> "$work/done"; echo ok
  else
    echo FAIL; tail -n 15 "$work/logs/$b.log"
  fi
done

needs() { awk -F'\t' -v b="$1" '$1 == b { print $2 }' "$HERE/needs"; }

n=0 okn=0
while read -r base; do
  [ -n "$base" ] || continue
  if grep -qx "$base" "$work/done"; then continue; fi
  if [ $((SECONDS - start)) -ge "$budget" ]; then log "time budget spent"; break; fi
  # twelve tries and nothing built: something general is wrong, not a package
  if [ "$n" -ge 12 ] && [ "$okn" -eq 0 ]; then
    log "the first $n packages all failed: stopping (see the logs)"
    for f in "$work"/logs/*.log; do echo "---- $f"; tail -n 15 "$f"; done | head -n 120
    exit 1
  fi
  n=$((n + 1)); t0=$SECONDS
  printf '   %-30s ' "$base"
  lf=$work/logs/$base.log
  if ! out=$(python3 "$HERE/import.py" --out "$work/recipes" --cache "$work/arch-repos" --sources "$work/sources" "$base" 2>&1); then
    echo "$out" > "$lf"; printf '%s\timport: %s\n' "$base" "$(echo "$out" | tail -n 1 | cut -c1-200)" >> "$work/failed"
    echo "IMPORT FAILED"; continue
  fi
  echo "$out" > "$lf"
  mapfile -t names < <(echo "$out" | sed -n 's/^[^ ]* *ok: //p' | tr ' ' '\n' | grep .)
  ok=1
  for name in "${names[@]}"; do
    build_dir "$work/recipes/$name" "$lf" || { ok=0; break; }
  done
  if [ "$ok" = 1 ]; then
    echo "$base" >> "$work/done"; okn=$((okn + 1)); printf 'ok   %5ds\n' $((SECONDS - t0))
  else
    printf '%s\t%s\n' "$base" "$(grep -v '^\s*$' "$lf" | tail -n 1 | cut -c1-200)" >> "$work/failed"
    printf 'FAIL %5ds\n' $((SECONDS - t0))
  fi
done < "$HERE/order"

total=$(grep -c . "$HERE/order")
echo
echo "built this pass: $okn (of $n tried)"
echo "overall: $(grep -c . "$work/done") done, $(grep -c . "$work/failed") failing, of $total package bases"
