#!/usr/bin/env bash
# Make a desktop system of the GraveyardOS base in $LFS: the edition's
# packages (built from Arch's recipes by the arch-build workflow) installed
# with Yone, then what makes it a desktop (login screen, services, look, the
# live session). iso/build.sh then makes GraveyardOS-<edition>.iso of it.
#
#   sudo YONE=<static yone> iso/desktop.sh <kde|hyprland> <arch-build work directory>
#
#   work directory   an arch-repo artifact: repo/ (signed packages and index)
#                    and archkey.pub.pem
#   GRAVEYARD_PARTIAL=1   install what the repository has of the edition and
#                    list what it lacks (to try the image before every
#                    package is built); otherwise a missing package fails
#   GRAVEYARD_DOTFILES    the dotfiles repository (default
#                    https://github.com/tfem1/dotfiles.git, branch yvis)
#
# The live session only (user "live", automatic login, the installer's
# window) goes into the initramfs' live files: the installed system never
# gets them (system/initramfs/init copies them over the live root).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
LFS=${LFS:-/mnt/lfs}
edition=${1:?kde or hyprland} work=$(realpath "${2:?arch-build work directory}")
yone=${YONE:?a static yone binary (YONE=...)}
case $edition in kde|hyprland) ;; *) echo "unknown edition $edition" >&2; exit 64 ;; esac
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -s "$work/repo/arch.index" ] || { echo "$work/repo/arch.index: no package repository" >&2; exit 1; }
log() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
inroot() { chroot "$LFS" /usr/bin/env -i HOME=/root TERM=xterm LC_ALL=C.UTF-8 PATH=/usr/bin:/usr/sbin "$@"; }

LFS=$LFS bash "$REPO/lfs/build.sh" mount > /dev/null
mnt=$LFS/var/lib/graveyard/desktop-repo
cleanup() {
  umount "$mnt" 2> /dev/null || true
  rmdir "$mnt" 2> /dev/null || true
  LFS=$LFS bash "$REPO/lfs/build.sh" umount > /dev/null 2>&1 || true
}
trap cleanup EXIT

# --- Yone, in the system for good -------------------------------------------
log "Yone"
install -m 755 "$yone" "$LFS/usr/bin/yone"
install -d "$LFS/etc/yone/keys" "$mnt"
install -m 644 "$work/archkey.pub.pem" "$LFS/etc/yone/keys/graveyard-dev.pub.pem"
mount --bind "$work/repo" "$mnt"
conf() { # server line
  cat > "$LFS/etc/yone/yone.conf" << EOF
# Yone's configuration (see 'yone help').
[options]
siglevel = required

# The packages of this image. The online repository comes with the first
# release; until then 'yone update' has nothing to fetch.
[arch]
$1
EOF
}
conf "server = file:///var/lib/graveyard/desktop-repo"
inroot yone --noconfirm --color never refresh > /dev/null

# --- the edition's packages --------------------------------------------------
# arch/targets: a "# Hyprland edition" block, a "# KDE edition" block and a
# "# both" block. Names are Arch's: @groups expanded, mapped to GraveyardOS's
# where the base has them under another name (arch/names), base ones left out.
section() { awk -v want="$1" '/^# /{ on = index($0, want) == 3 } on && !/^#/' "$REPO/arch/targets"; }
case $edition in kde) title="KDE edition" ;; hyprland) title="Hyprland edition" ;; esac
mapfile -t wanted < <({ section "$title"; section both; } | tr ' ' '\n' | grep . |
  xargs python3 "$REPO/arch/closure.py" --expand |
  awk 'NR == FNR { if ($0 !~ /^#/ && NF == 2) map[$1] = $2; next } { print (($0 in map) ? map[$0] : $0) }' "$REPO/arch/names" - |
  grep -vxF -f <(grep -v '^#' "$REPO/arch/base-provides" | tr ' ' '\n' | grep .) | sort -u)
mapfile -t offered < <(inroot yone --color never list | sed 's|^[^/]*/||; s| .*||')
names=() missing=()
for n in "${wanted[@]}"; do
  if printf '%s\n' "${offered[@]}" | grep -qxF "$n"; then names+=("$n"); else missing+=("$n"); fi
done
log "$edition: ${#wanted[@]} packages wanted, ${#names[@]} in the repository"
if [ "${#missing[@]}" -gt 0 ]; then
  echo "   not built yet: ${missing[*]}"
  [ "${GRAVEYARD_PARTIAL:-0}" = 1 ] || { echo "missing packages (GRAVEYARD_PARTIAL=1 to go on)" >&2; exit 1; }
fi
inroot yone --noconfirm --color never install "${names[@]}"
has() { [ -e "$LFS/$1" ]; }

# --- what makes it a desktop -------------------------------------------------
log "services and login screen"
for unit in sddm NetworkManager bluetooth power-profiles-daemon; do
  has "usr/lib/systemd/system/$unit.service" && inroot systemctl enable "$unit" > /dev/null 2>&1 && echo "   $unit"
done
inroot systemctl set-default graphical.target > /dev/null
install -d "$LFS/etc/sddm.conf.d"
case $edition in
  kde) session=plasma.desktop ;;
  hyprland) session=hyprland.desktop ;;
esac
# GraveyardOS's login screen (branding/sddm/graveyard), with the Dunes
theme=graveyard
tdir=$LFS/usr/share/sddm/themes/graveyard
rm -rf "$tdir"; install -d "$tdir/backgrounds"
cp -R "$REPO/branding/sddm/graveyard/." "$tdir/"
install -m 644 "$REPO/branding/wallpapers/Graveyard-Dunes/contents/images_dark/2560x1600.jpg" "$tdir/backgrounds/night.jpg"
install -m 644 "$REPO/branding/wallpapers/Graveyard-Dunes/contents/images/2560x1600.jpg" "$tdir/backgrounds/day.jpg"
echo "   login screen: graveyard"
cat > "$LFS/etc/sddm.conf.d/10-graveyard.conf" << EOF
# GraveyardOS $edition
[Theme]
Current=$theme
CursorTheme=$([ "$edition" = kde ] && echo breeze_cursors || echo Adwaita)
EOF

# what the installer offers (installer/graveyard_install.py)
install -d "$LFS/usr/share/graveyard/installer"
echo "$session" > "$LFS/usr/share/graveyard/installer/desktop-$edition"

log "look"
install -d "$LFS/usr/share/wallpapers"
rm -rf "$LFS/usr/share/wallpapers/Graveyard-Dunes"
cp -R "$REPO/branding/wallpapers/Graveyard-Dunes" "$LFS/usr/share/wallpapers/"
install -Dm644 "$REPO/branding/graveyard-icon.svg" "$LFS/usr/share/pixmaps/graveyard.svg"
# Fastfetch: the owner's Eye of Horus, in white (a user's own config wins)
install -Dm644 "$REPO/branding/fastfetch/graveyard.txt" "$LFS/usr/share/graveyard/fastfetch/graveyard.txt"
install -Dm644 "$REPO/branding/fastfetch/config.jsonc" "$LFS/etc/xdg/fastfetch/config.jsonc"
install -Dm644 "$REPO/branding/graveyard-icon.svg" "$LFS/usr/share/icons/hicolor/scalable/apps/graveyard.svg"
if [ "$edition" = kde ]; then
  # A look-and-feel of its own: Breeze Dark with the Dunes, the default for
  # every new user (kdeglobals).
  laf=$LFS/usr/share/plasma/look-and-feel
  if [ -d "$laf/org.kde.breezedark.desktop" ]; then
    rm -rf "$laf/org.graveyardos.desktop"
    cp -R "$laf/org.kde.breezedark.desktop" "$laf/org.graveyardos.desktop"
    python3 - "$laf/org.graveyardos.desktop" << 'PY'
import json, os, re, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, "metadata.json")))
m["KPlugin"].update({"Id": "org.graveyardos.desktop", "Name": "GraveyardOS",
                     "Description": "Breeze Dark with the Graveyard Dunes", "Authors": [{"Name": "tfem1"}]})
m["KPlugin"] = {k: v for k, v in m["KPlugin"].items() if not re.match(r"(Name|Description)\[", k)}
json.dump(m, open(os.path.join(d, "metadata.json"), "w"), indent=1)
p = os.path.join(d, "contents", "defaults")
t = open(p).read()
t = re.sub(r"(?ms)^\[Wallpaper\].*?(?=^\[|\Z)", "", t)
open(p, "w").write(t.rstrip() + "\n\n[Wallpaper]\nImage=Graveyard-Dunes\n")
PY
    install -d "$LFS/etc/xdg"
    printf '[KDE]\nLookAndFeelPackage=org.graveyardos.desktop\n' > "$LFS/etc/xdg/kdeglobals"
    echo "   look and feel org.graveyardos.desktop (Breeze Dark, Graveyard Dunes)"
  else
    echo "   no Breeze Dark look-and-feel: the wallpaper is installed, not set" >&2
  fi
else
  # The owner's dotfiles, for every new user (/etc/skel), from
  # /usr/share/tfem; its icons and font are downloaded here (the system has
  # no HTTPS client), its configuration and theme generated in the system.
  dots=${GRAVEYARD_DOTFILES:-https://github.com/tfem1/dotfiles.git}
  rm -rf "$LFS/usr/share/tfem"
  git clone -q --depth 1 --branch "${GRAVEYARD_DOTFILES_BRANCH:-yvis}" "$dots" "$LFS/usr/share/tfem"
  rm -rf "$LFS/usr/share/tfem/.git"
  HOME=$LFS/etc/skel TFEM_DIR=$LFS/usr/share/tfem bash "$LFS/usr/share/tfem/install.sh" \
    --yes --no-tui --no-packages --no-sddm --no-wallpapers > "$LFS/var/log/graveyard-dotfiles-host.log" 2>&1 \
    || { tail -n 20 "$LFS/var/log/graveyard-dotfiles-host.log"; exit 1; }
  inroot env HOME=/etc/skel TFEM_DIR=/usr/share/tfem bash /usr/share/tfem/install.sh \
    --yes --no-tui --no-packages --no-wallpapers --no-icons --no-fonts > "$LFS/var/log/graveyard-dotfiles.log" 2>&1 \
    || { tail -n 20 "$LFS/var/log/graveyard-dotfiles.log"; exit 1; }
  # links made from this host point into $LFS: make them the system's
  find "$LFS/etc/skel" -type l | while read -r l; do
    t=$(readlink "$l"); case $t in "$LFS"/*) ln -sfn "${t#"$LFS"}" "$l" ;; esac
  done
  rm -rf "$LFS/etc/skel/.local/state/tfem"
  if grep -rlI -- "$LFS" "$LFS/etc/skel" "$LFS/usr/share/tfem" 2> /dev/null | head -n 5 | grep .; then
    echo "the files above name the build directory $LFS" >&2; exit 1
  fi
  # the Dunes, where the dotfiles look for wallpapers
  install -d "$LFS/etc/skel/Pictures/Wallpapers/GraveyardOS"
  cp "$LFS/usr/share/wallpapers/Graveyard-Dunes/contents/images/"* "$LFS/etc/skel/Pictures/Wallpapers/GraveyardOS/" 2> /dev/null || true
  echo "   dotfiles of tfem1/dotfiles (branch ${GRAVEYARD_DOTFILES_BRANCH:-yvis}) in /etc/skel"
fi

# --- the live session --------------------------------------------------------
log "live session"
live=$LFS/usr/share/graveyard/initramfs/live
install -d "$live/etc/sddm.conf.d" "$live/etc/systemd/system/multi-user.target.wants" \
  "$live/etc/xdg/autostart" "$live/usr/share/applications"
cat > "$live/etc/sddm.conf.d/90-live.conf" << EOF
# the live session logs in by itself
[Autologin]
User=live
Session=$session
EOF
cat > "$live/etc/systemd/system/graveyard-live-user.service" << 'EOF'
[Unit]
Description=The user of the GraveyardOS live session
Before=display-manager.service systemd-user-sessions.service
ConditionPathExists=!/home/live

[Service]
Type=oneshot
ExecStart=/usr/sbin/useradd -m -c "Live session" -G wheel,audio,video,input -s /bin/bash live
ExecStart=/usr/bin/passwd -d live
ExecStart=/bin/sh -c 'echo "live ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/live && chmod 440 /etc/sudoers.d/live'

[Install]
WantedBy=multi-user.target
EOF
ln -sfn ../graveyard-live-user.service "$live/etc/systemd/system/multi-user.target.wants/graveyard-live-user.service"
# the installer: its window, on the desktop and in the menu
install -m 755 "$REPO/installer/graveyard-installer-window" "$live/usr/bin/graveyard-installer-window"
install -m 644 "$REPO/installer/window.qml" "$live/usr/lib/graveyard/installer/window.qml"
cat > "$live/usr/share/applications/graveyard-installer.desktop" << 'EOF'
[Desktop Entry]
Type=Application
Name=Install GraveyardOS
Comment=Install GraveyardOS on this computer
Exec=graveyard-installer-window
Icon=graveyard
Terminal=false
Categories=System;
EOF
cp "$live/usr/share/applications/graveyard-installer.desktop" "$live/etc/xdg/autostart/"
echo "   user live, automatic login ($session), installer window"

log "initramfs (with the live session)"
inroot graveyard-mkinitramfs "$(cat "$LFS/.graveyard/kernel-release")" > /dev/null

conf "# server = (none yet)"
rm -rf "$LFS/var/cache/yone/pkg"
log "$edition desktop ready: $(du -sh --exclude=sources --exclude=.graveyard "$LFS" 2> /dev/null | cut -f1) on disk"
