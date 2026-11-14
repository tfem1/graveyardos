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
#           index), key.*, done, failed, skipped; plus logs/ for this pass
#
# Each package base of arch/order is imported (arch/import.py), built with
# Yone in a chroot, then installed. A base whose build dependency failed is
# skipped, and the pass goes on. Exit status 0 when the budget ran out or
# nothing was left; the caller compares done/failed/skipped with arch/order.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
sys=$(realpath "${1:?system}") work=${2:?work} budget=${3:?seconds}
yone=${YONE:?a static yone binary (YONE=...)}
mkdir -p "$work" && work=$(realpath "$work")
mkdir -p "$work/repo" "$work/logs" "$work/recipes" "$work/arch-repos" "$work/sources"
touch "$work/done" "$work/failed" "$work/skipped"
: > "$work/logs/.keep"
start=$SECONDS
log() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }

LFS=$sys bash "$REPO/lfs/build.sh" mount > /dev/null
trap 'LFS=$sys bash "$REPO/lfs/build.sh" umount > /dev/null 2>&1 || true' EXIT
install -m 755 "$yone" "$sys/usr/local/bin/yone"
in() { chroot "$sys" /usr/bin/env -i HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin:/usr/local/bin "$@"; }

# The development key of this chain (the repository is not a release).
if [ ! -s "$work/archkey.key" ]; then
  in /usr/local/bin/yone keygen /tmp/archkey > /dev/null
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
index() { in /usr/local/bin/yone repo-index /packages/repo --name arch --sign /packages/key.key --color never > /dev/null; }

# What earlier passes built goes back into the system.
if ls "$work"/repo/*.yon > /dev/null 2>&1; then
  log "installing what earlier passes built"
  index
  in /usr/local/bin/yone --noconfirm --overwrite --color never refresh > /dev/null
  mapfile -t built < <(in /usr/local/bin/yone --color never list | sed 's|^[^/]*/||; s| .*||')
  in /usr/local/bin/yone --noconfirm --overwrite --color never install "${built[@]}" > "$work/logs/reinstall.log" 2>&1 \
    || { echo "reinstall failed:"; tail -n 20 "$work/logs/reinstall.log"; exit 1; }
fi

needs() { awk -F'\t' -v b="$1" '$1 == b { print $2 }' "$HERE/needs"; }
state() { grep -qx "$1" "$work/done" "$work/failed" "$work/skipped" 2> /dev/null; }
listed() { cut -f1 "$work/$2" | grep -qx "$1"; }

n=0
while read -r base; do
  [ -n "$base" ] || continue
  if state "$base" || listed "$base" failed || listed "$base" skipped; then continue; fi
  if [ $((SECONDS - start)) -ge "$budget" ]; then log "time budget spent"; break; fi
  blocked=""
  for d in $(needs "$base"); do
    if listed "$d" failed || listed "$d" skipped; then blocked=$d; break; fi
  done
  if [ -n "$blocked" ]; then printf '%s\tneeds %s\n' "$base" "$blocked" >> "$work/skipped"; continue; fi
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
    d=$sys/packages/recipes/$name
    rm -rf "$d"; mkdir -p "$d/.yone-sources"; cp -a "$work/recipes/$name/." "$d/"
    # the sources the importer downloaded, where yone looks first
    for f in $(sed -n 's|^source = \([^:/]*\)::.*|\1|p; s|^source = .*/\([^/]*\)$|\1|p' "$d/YONEBUILD"); do
      [ -e "$work/sources/$f" ] && cp "$work/sources/$f" "$d/.yone-sources/$f"
    done
    if ! timeout --kill-after=1m "${ARCH_PACKAGE_TIMEOUT:-150m}" setsid --wait chroot "$sys" /usr/bin/env -i \
        HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin:/usr/local/bin MAKEFLAGS="-j$(nproc)" \
        /usr/local/bin/yone build "/packages/recipes/$name" --out /packages/repo --sign /packages/key.key \
        --compress zstd --color never < /dev/null >> "$lf" 2>&1; then
      ok=0; break
    fi
  done
  if [ "$ok" = 1 ]; then
    index
    if in /usr/local/bin/yone --noconfirm --overwrite --color never refresh >> "$lf" 2>&1 \
       && in /usr/local/bin/yone --noconfirm --overwrite --color never install "${names[@]}" >> "$lf" 2>&1; then
      echo "$base" >> "$work/done"; printf 'ok   %5ds\n' $((SECONDS - t0))
      continue
    fi
    printf '%s\tinstall: %s\n' "$base" "$(tail -n 1 "$lf" | cut -c1-200)" >> "$work/failed"; echo "INSTALL FAILED"
  else
    printf '%s\tbuild: %s\n' "$base" "$(grep -v '^\s*$' "$lf" | tail -n 1 | cut -c1-200)" >> "$work/failed"
    printf 'FAIL %5ds\n' $((SECONDS - t0))
  fi
done < "$HERE/order"

total=$(grep -c . "$HERE/order")
echo
echo "this pass: $n tried; overall: $(grep -c . "$work/done") built, $(grep -c . "$work/failed") failed, $(grep -c . "$work/skipped") skipped, of $total"
