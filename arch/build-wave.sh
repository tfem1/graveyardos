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
# ARCH_ONLY=<file>: build only the package bases listed in it (one per line,
# in that order), a shard of a parallel wave (arch-wave workflow).
# ARCH_LONG=1: a base of arch/long, built over several jobs (arch-long
# workflow): the work directory of the earlier job, in ARCH_LONG_WORK (same
# file system as the system), goes back in place and the build resumes
# (YONE_RESUME).
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
python3 "$HERE/repair-done.py" "$work"   # bases whose packages left the repository
# a package that failed may build now that more is built: every pass tries
# again all that is not done
mv "$work/failed" "$work/failed.before"; : > "$work/failed"
# what the base provides counts as built (glibc, gcc...: arch/needs names them)
{ cat "$work/done"; grep -v '^#' "$HERE/base-names" | tr ' ' '\n' | grep .; } | awk '!seen[$0]++' > "$work/done.new"
mv "$work/done.new" "$work/done"
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
# The network for what PKGBUILDs fetch in prepare(), as on Arch's build
# machines: cargo fetch (Rust crates), go mod download... The host's name
# server and certificate authorities (the base has none of its own).
cp -L /etc/resolv.conf "$sys/etc/resolv.conf"
mkdir -p "$sys/etc/ssl/graveyard-host-certs"
mount --bind -o ro /etc/ssl/certs "$sys/etc/ssl/graveyard-host-certs"
trap 'umount "$sys/packages/repo" "$sys/etc/ssl/graveyard-host-certs" 2> /dev/null || true; LFS=$sys bash "$REPO/lfs/build.sh" umount > /dev/null 2>&1 || true' EXIT
cat > "$sys/etc/yone/yone.conf" << 'EOF'
[options]
siglevel = required

[arch]
server = file:///packages/repo
EOF
# CMake's GNUInstallDirs installs libraries in lib, not lib64, on a system
# that says it is Arch: Arch's recipes expect /usr/lib
touch "$sys/etc/arch-release"
index() { inroot /usr/local/bin/yone repo-index /packages/repo --name arch --sign /packages/key.key --color never > /dev/null; }

# What earlier passes built goes back into the system.
if ls "$work"/repo/*.yon > /dev/null 2>&1; then
  log "installing what earlier passes built"
  index
  inroot /usr/local/bin/yone --noconfirm --overwrite --color never refresh > /dev/null
  mapfile -t built < <(inroot /usr/local/bin/yone --color never list | sed 's|^[^/]*/||; s| .*||')
  # One at a time: a package built while what it needs to run was not (it
  # built all the same) cannot be installed yet; it leaves the repository
  # (rebuilt when its base is tried again) so it shadows nothing, such as a
  # bootstrap of the same name.
  left_out=() installed_now=0
  for p in "${built[@]}"; do
    if inroot /usr/local/bin/yone --noconfirm --overwrite --color never install "$p" > "$work/logs/reinstall.one" 2>&1; then
      installed_now=$((installed_now + 1))
    else
      left_out+=("$p")
      printf '   not installable: %s: %s\n' "$p" "$(grep -E 'error' "$work/logs/reinstall.one" | head -n 1 | cut -c1-200)"
    fi
    cat "$work/logs/reinstall.one" >> "$work/logs/reinstall.log"
  done
  if [ "${#left_out[@]}" -gt 0 ]; then
    for p in "${left_out[@]}"; do
      # the newest one is what failed; an older one (a bootstrap) stays
      newest=$(ls "$work"/repo/"$p"-[0-9]*.yon 2> /dev/null | sort -V | tail -n 1)
      [ -n "$newest" ] && rm -f "$newest" "$newest.sig"
    done
    index
    inroot /usr/local/bin/yone --noconfirm --overwrite --color never refresh > /dev/null
    # what was hidden behind them (bootstraps), installed now
    for p in "${left_out[@]}"; do
      inroot /usr/local/bin/yone --noconfirm --overwrite --color never install "$p" >> "$work/logs/reinstall.log" 2>&1 \
        && installed_now=$((installed_now + 1)) || true
    done
  fi
  echo "   reinstalled $installed_now of ${#built[@]}; out of the repository until their dependencies are built: ${left_out[*]:-none}"
fi

# build one recipe directory in the chroot and install what it made
build_dir() { # recipe dir (host path), log
  local src=$1 lf=$2 name d
  name=$(sed -n 's/^name = //p' "$src/YONEBUILD")
  d=$sys/packages/recipes/$name
  rm -rf "$d"; mkdir -p "$d/.yone-sources"; cp -a "$src/." "$d/"
  # ARCH_LONG: the work directory of the earlier job, where the build resumes
  if [ -n "${ARCH_LONG:-}" ] && [ -d "${ARCH_LONG_WORK:-/nonexistent}/.yone-work" ]; then
    mv "$ARCH_LONG_WORK/.yone-work" "$d/.yone-work"
  fi
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
  local before
  before=$(ls "$work/repo")
  timeout --kill-after=1m "${ARCH_PACKAGE_TIMEOUT:-150m}" setsid --wait chroot "$sys" /usr/bin/env -i \
    SSL_CERT_FILE=/etc/ssl/graveyard-host-certs/ca-certificates.crt \
    CARGO_HTTP_CAINFO=/etc/ssl/graveyard-host-certs/ca-certificates.crt \
    GIT_SSL_CAINFO=/etc/ssl/graveyard-host-certs/ca-certificates.crt \
    HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin:/usr/local/bin MAKEFLAGS="-j$(nproc)" \
    ${ARCH_LONG:+YONE_RESUME=1} \
    /usr/local/bin/yone build "/packages/recipes/$name" --out /packages/repo --sign /packages/key.key \
    --compress zstd --color never < /dev/null >> "$lf" 2>&1 || return 1
  index
  if inroot /usr/local/bin/yone --noconfirm --overwrite --color never refresh >> "$lf" 2>&1 &&
    inroot /usr/local/bin/yone --noconfirm --overwrite --color never install "$name" >> "$lf" 2>&1; then
    return 0
  fi
  # built but not installable (what it needs to run is not built yet): out of
  # the repository, so it shadows nothing (a bootstrap of the same name...)
  comm -13 <(echo "$before") <(ls "$work/repo") | grep -v '\.index' | sed "s|^|$work/repo/|" | xargs -r rm -f
  index
  inroot /usr/local/bin/yone --noconfirm --color never refresh > /dev/null 2>&1 || true
  return 1
}

# The base system, declared to Yone: one package that provides every base
# package's name, so packages depending on glibc, perl... install.
# Built again when arch/base-provides changes (its checksum in the version).
bp_sum=$(grep -v '^#' "$HERE/base-provides" | cksum | cut -d' ' -f1)
if ! grep -qx "graveyard-base-provides@$bp_sum" "$work/done"; then
  r=$work/recipes/graveyard-base-provides; mkdir -p "$r"
  rm -f "$work"/repo/graveyard-base-provides-*.yon "$work"/repo/graveyard-base-provides-*.yon.sig  # the newest is the only one
  {
    echo "name = graveyard-base-provides"; echo "version = 2.$bp_sum"; echo "release = 1"
    echo "desc = The GraveyardOS base system, installed by the image itself"
    echo "provides = $(grep -v '^#' "$HERE/base-provides" | tr '\n' ' ' | tr -s ' ')"
    echo; echo "@package"; echo 'mkdir -p "$PKGDIR/usr/share/graveyard"; echo base > "$PKGDIR/usr/share/graveyard/base-provides"'
    # Arch's python package is python 3 under the name "python"
    echo 'mkdir -p "$PKGDIR/usr/bin"; ln -s python3 "$PKGDIR/usr/bin/python"'
  } > "$r/YONEBUILD"
  build_dir "$r" "$work/logs/graveyard-base-provides.log" || { tail -n 20 "$work/logs/graveyard-base-provides.log"; exit 1; }
  echo "graveyard-base-provides@$bp_sum" >> "$work/done"
fi

# Tools Arch builds with themselves, from arch/bootstrap first.
for r in "$HERE"/bootstrap/git "$HERE"/bootstrap/python-pyproject-hooks "$HERE"/bootstrap/python-installer "$HERE"/bootstrap/python-build "$HERE"/bootstrap/cmake "$HERE"/bootstrap/rust "$HERE"/bootstrap/go "$HERE"/bootstrap/gengetopt; do
  b=$(basename "$r")
  grep -qx "$b" "$work/done" && continue
  grep -qx "bootstrap:$b" "$work/done" && continue
  printf '   %-30s ' "$b (bootstrap)"
  if build_dir "$r" "$work/logs/$b.log"; then
    sed -n 's/^# replaces: //p' "$r/YONEBUILD" | tr ' ' '\n' | { grep . || true; } >> "$work/done"
    # without "# replaces:", the base is still built from source later
    echo "bootstrap:$b" >> "$work/done"; echo ok
  else
    echo FAIL; tail -n 15 "$work/logs/$b.log"
  fi
done

# what a base needs built first, less what arch/drop leaves out of it
needs() {
  awk -F'\t' -v b="$1" '
    FILENAME ~ /drop$/ { if ($0 !~ /^#/ && $1 == b) gone[$2] = 1; next }
    $1 == b { n = split($2, d, " "); for (i = 1; i <= n; i++) if (!(d[i] in gone)) print d[i] }
  ' "$HERE/drop" "$HERE/needs"
}

# GraveyardOS's own recipes (arch/extra), built as soon as what their
# "# after:" line names is built; once per pass.
extras_tried=" "
build_extras() {
  local r n after a ready
  for r in "$HERE"/extra/*/; do
    [ -f "$r/YONEBUILD" ] || continue
    n=$(basename "$r")
    # built again when its version or release changes
    mark="extra:$n@$(sed -n 's/^version = //p' "$r/YONEBUILD")-$(sed -n 's/^release = //p' "$r/YONEBUILD")"
    grep -qx "$mark" "$work/done" && continue
    case $extras_tried in *" $n "*) continue ;; esac
    after=$(sed -n 's/^# after: //p' "$r/YONEBUILD")
    ready=1
    for a in $after; do grep -qx "$a" "$work/done" || ready=0; done
    [ "$ready" = 1 ] || continue
    extras_tried="$extras_tried$n "
    printf '   %-30s ' "$n (extra)"
    if build_dir "$r" "$work/logs/$n.log"; then
      grep -qx "$n" "$work/done" || echo "$n" >> "$work/done"
      echo "$mark" >> "$work/done"; okn=$((okn + 1)); echo ok
    else
      printf '%s\t%s\n' "$n" "$(grep -v '^\s*$' "$work/logs/$n.log" | tail -n 1 | cut -c1-200)" >> "$work/failed"
      echo FAIL
    fi
  done
}

n=0 okn=0 waiting=0
build_extras
# A shard line may name, after the base, the dependencies of its cycle it is
# built without (plan-wave.py breaks each dependency cycle at one base)
while read -r base relax; do
  [ -n "$base" ] || continue
  if grep -qx "$base" "$work/done"; then continue; fi
  # a build of its own (arch-long), unless this is it
  if [ -z "${ARCH_LONG:-}" ] && grep -qx "$base" <(grep -v '^#' "$HERE/long" | cut -f1); then continue; fi
  # what the GraveyardOS base already is (glibc, gcc...): never rebuilt
  if grep -v '^#' "$HERE/base-names" | tr ' ' '\n' | grep -qx "$base"; then
    echo "$base" >> "$work/done"; continue
  fi
  if [ $((SECONDS - start)) -ge "$budget" ]; then log "time budget spent"; break; fi
  # a base whose build or run dependencies are not built yet waits (it is
  # not a failure: a later pass, or another shard's, builds them)
  missing_need=
  for d in $(needs "$base"); do
    case " $relax " in *" $d "*) continue ;; esac
    grep -qx "$d" "$work/done" || { missing_need=$d; break; }
  done
  [ -z "$relax" ] || [ -n "$missing_need" ] || echo "   $base: built before $relax (a dependency cycle)"
  if [ -n "$missing_need" ]; then waiting=$((waiting + 1)); continue; fi
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
    printf '%s\t%s\n' "$base" "${names[*]}" >> "$work/made"   # its packages' names (repair-done.py)
    build_extras
  else
    # "relaxed": it lacked part of its cycle, so plan-wave.py tries it again
    # once that part is built
    rx=; for d in $relax; do grep -qx "$d" "$work/done" || rx=relaxed; done
    printf '%s\t%s%s\n' "$base" "$(grep -v '^\s*$' "$lf" | tail -n 1 | cut -c1-200 | tr '\t' ' ')" "${rx:+	$rx}" >> "$work/failed"
    printf 'FAIL %5ds\n' $((SECONDS - t0))
  fi
done < "${ARCH_ONLY:-$HERE/order}"

total=$(grep -c . "$HERE/order")
[ -z "${ARCH_ONLY:-}" ] || echo "this shard: $(grep -c . "$ARCH_ONLY") package bases"
echo
echo "built this pass: $okn (of $n tried; $waiting waiting for their dependencies)"
echo "overall: $(grep -cxF -f "$HERE/order" "$work/done") done, $(grep -c . "$work/failed") failing, of $total package bases"
