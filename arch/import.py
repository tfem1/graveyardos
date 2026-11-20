#!/usr/bin/env python3
"""Turn Arch Linux package recipes (PKGBUILD, 0BSD) into GraveyardOS recipes.

    python3 arch/import.py --out DIR [--cache DIR] [--sources DIR] pkgbase ...

For each package base: its packaging repository is fetched from
gitlab.archlinux.org, the PKGBUILD is evaluated by bash (with nothing run but
the assignments), and one YONEBUILD is written per package it makes
(DIR/<pkgname>/YONEBUILD), with the PKGBUILD's own local files beside it.

The recipe runs the PKGBUILD's prepare(), build() and package() functions as
makepkg would: $srcdir and $pkgdir are Yone's $SRCDIR and $PKGDIR, and the
compiler flags are Arch's (makepkg.conf, x86-64 baseline). Sources keep their
URLs; Arch's checksums are checked and the SHA-256 Yone requires is computed
from the downloaded file (--sources keeps the downloads).

Dependency names are Arch's, mapped to GraveyardOS's base where they differ
(arch/names); library (soname) dependencies are dropped: GraveyardOS records
packages, not libraries. Git sources are cloned here and packed with their history. A package the
importer cannot convert (Subversion, Mercurial or Bazaar sources,
an architecture other than x86_64) is reported and skipped.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GIT = "https://gitlab.archlinux.org/archlinux/packaging/packages/{}.git"

# makepkg.conf (Arch, x86_64), the flags every package is built with
FLAGS = r'''export CHOST=x86_64-pc-linux-gnu
export CFLAGS="-march=x86-64 -mtune=generic -O2 -pipe -fno-plt -fexceptions -Wp,-D_FORTIFY_SOURCE=3 -Wformat -Werror=format-security -fstack-clash-protection -fcf-protection -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer"
export CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
export LDFLAGS="-Wl,-O1 -Wl,--sort-common -Wl,--as-needed -Wl,-z,relro -Wl,-z,now -Wl,-z,pack-relative-relocs"
export LTOFLAGS="-flto=auto"
export RUSTFLAGS="-Cforce-frame-pointers=yes"
export DEBUG_CFLAGS="-g" DEBUG_CXXFLAGS="-g"
# python -m build without --no-isolation fetches the build dependencies from
# PyPI (Arch builds with the network; this system has none): the installed
# ones are used. It also checks the build dependencies' versions against what is
# installed; Arch's packages are built against Arch's of the day, as these:
# a newer flit-core must not stop python-mdurl (Arch's makepkg has the same
# packages, built before the update)
python() {
  if [ "${1:-}" = -m ] && [ "${2:-}" = build ]; then shift 2; command python -m build --skip-dependency-check --no-isolation "$@"
  else command python "$@"; fi
}
python3() {
  if [ "${1:-}" = -m ] && [ "${2:-}" = build ]; then shift 2; command python3 -m build --skip-dependency-check --no-isolation "$@"
  else command python3 "$@"; fi
}
# bsdtar (libarchive) is on every Arch build machine, not on GraveyardOS's
# base: extraction (-x) of tar archives with GNU tar, of zip with Python
command -v bsdtar > /dev/null 2>&1 || bsdtar() {
  local f="" C="" a x=0
  local -a rest=()
  case ${1:-} in -*|"") ;; *x*f*) x=1; f=$2; shift 2 ;; *x*) x=1; shift ;; esac  # bsdtar xf FILE
  while [ $# -gt 0 ]; do
    case $1 in
      -x|-xf|-xvf|-xpf) x=1; [ "$1" = -x ] || { f=$2; shift; } ;;
      -f) f=$2; shift ;;
      -C) C=$2; shift ;;
      *) rest+=("$1") ;;
    esac
    shift
  done
  [ "$x" = 1 ] || { echo "bsdtar: only extraction is offered here" >&2; return 1; }
  case $f in
    *.zip) python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$f" "${C:-.}" ;;
    *) tar -xf "$f" ${C:+-C "$C"} "${rest[@]}" ;;
  esac
}
# c89 and c99, POSIX's names for the C compiler: scripts of Arch's gcc
# package, not of LFS's (libfreeaptx's Makefile compiles with c99)
for _cc in c89 c99; do
  command -v $_cc > /dev/null 2>&1 && continue
  mkdir -p "${SRCDIR%/*}/graveyard-bin"
  printf '#!/bin/sh\nexec gcc -std=%s "$@"\n' $_cc > "${SRCDIR%/*}/graveyard-bin/$_cc"
  chmod 755 "${SRCDIR%/*}/graveyard-bin/$_cc"
  case :$PATH: in *":${SRCDIR%/*}/graveyard-bin:"*) ;; *) PATH="${SRCDIR%/*}/graveyard-bin:$PATH" ;; esac
done
# Arch's meson wrapper (/usr/bin/arch-meson of its meson package), which
# PKGBUILDs call instead of meson; --libdir lib as meson picks on Arch, where
# /usr/lib64 is a link; --auto-features auto, not enabled: features of the
# check dependencies, which are not built here (libdrm's cairo tests), are
# left out instead of failing
arch-meson() {
  meson setup --prefix /usr --libdir lib --libexecdir lib --sbindir bin --buildtype plain \
    --auto-features auto --wrap-mode nodownload -D b_pie=true -D python.bytecompile=1 "$@"
}'''

# Evaluate a PKGBUILD: its variables as JSON, its functions as text.
EVAL = r'''
set -e
cd "$1"
CARCH=x86_64 CHOST=x86_64-pc-linux-gnu
source ./PKGBUILD >/dev/null
arr() { local n=$1; declare -p "$n" >/dev/null 2>&1 || { echo "[]"; return; }
  local -n a=$n; printf '['; local f=1; for x in "${a[@]}"; do
  [ $f = 1 ] || printf ','; f=0; printf '%s' "$x" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")'; done; printf ']'; }
str() { printf '%s' "${!1-}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")'; }
echo "{"
for v in pkgbase pkgver pkgrel epoch pkgdesc url; do printf '"%s": %s,\n' $v "$(str $v)"; done
for v in pkgname arch license depends makedepends checkdepends optdepends provides conflicts replaces backup \
         source source_x86_64 noextract sha256sums sha256sums_x86_64 sha512sums sha512sums_x86_64 b2sums b2sums_x86_64 \
         md5sums options; do
  printf '"%s": %s,\n' $v "$(arr $v)"
done
printf '"_": null}\n'
'''

TIDY = r'''graveyard_tidy() {
  local opts=" ${options[*]-} " f
  cd "$pkgdir" || return
  rm -f usr/share/info/dir usr/info/dir
  find . -name .packlist -delete
  find . -name '*.pod' -path '*/perl*' -delete
  case $opts in *" libtool "*) ;; *) find . -name '*.la' -type f -delete ;; esac
  case $opts in *" staticlibs "*) ;; *)
    find . -name '*.a' -type f | while read -r f; do
      if [ -e "${f%.a}.so" ]; then rm -f "$f"; fi   # (a bare && ends set -e scripts)
    done ;;
  esac
  case $opts in *" !docs "*) rm -rf usr/share/doc usr/doc usr/share/gtk-doc ;; esac
  case $opts in *" !emptydirs "*) find . -depth -type d -empty -delete ;; esac
  case $opts in *" !strip "*) ;; *)
    find . -type f -perm -u+w | while read -r f; do
      [ "$(head -c 4 "$f" 2> /dev/null | od -An -c | tr -d ' ')" = '177ELF' ] || continue
      # a file strip does not know (guile's compiled .go: ELF of no
      # machine) is left as it is, as makepkg does; under set -e a failed
      # strip would end the build with no message
      case $f in
        *.so | *.so.*) strip --strip-unneeded "$f" 2> /dev/null || true ;;
        *.a | *.o) strip --strip-debug "$f" 2> /dev/null || true ;;
        *) strip --strip-all "$f" 2> /dev/null || true ;;
      esac
    done ;;
  esac
  cd "$srcdir" || return
}
graveyard_tidy'''

SONAME = re.compile(r"\.so(=|$)")


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def run(cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, capture_output=True, text=True, **kw)


def names_map():
    m = {}
    path = os.path.join(HERE, "names")
    if os.path.exists(path):
        for line in open(path):
            line = line.split("#")[0].split()
            if len(line) == 2:
                m[line[0]] = line[1]
            elif len(line) == 1:
                m[line[0]] = None  # provided by the base, no package to depend on
    return m


def bare(dep):
    return re.split(r"[<>=]", dep, maxsplit=1)[0]


def fixes(pkgbase):
    """arch/fixes: shell lines for this base, run after its PKGBUILD is read
    (flags for a newer compiler...)."""
    out = []
    for l in open(os.path.join(HERE, "fixes")):
        f = l.rstrip("\n").split("\t", 1)
        if len(f) == 2 and not l.startswith("#") and f[0] == pkgbase:
            out.append(f[1])
    return out


def drops(pkgbase):
    """What arch/drop leaves out of this base."""
    out = set()
    for l in open(os.path.join(HERE, "drop")):
        f = l.rstrip("\n").split("\t")
        if len(f) >= 2 and not l.startswith("#") and f[0] == pkgbase:
            out.add(f[1])
    return out


def map_deps(deps, nmap, own):
    out = []
    for d in deps:
        n = bare(d)
        if SONAME.search(d) or n in own:
            continue
        if n in nmap:
            if nmap[n] is None:
                continue
            n = nmap[n]
        if n not in out:
            out.append(n)
    return out


def gitlab_path(pkgbase):
    """The packaging repository's name on gitlab.archlinux.org, as Arch's
    devtools make it (gitlab_project_name_to_path): crypto++ is
    cryptoplusplus, libsigc++ libsigc-plus-plus... and tree, a name GitLab
    reserves, unix-tree."""
    n = re.sub(r"([a-zA-Z0-9])\+([a-zA-Z])", r"\1-\2", pkgbase)
    n = n.replace("+", "plus")
    n = re.sub(r"[^a-zA-Z0-9_\-.]", "-", n)
    n = re.sub(r"[_\-]{2,}", "-", n)
    return "unix-tree" if n == "tree" else n


def fetch_repo(pkgbase, cache):
    path = os.path.join(cache, pkgbase)
    if os.path.isdir(os.path.join(path, ".git")):
        return path
    run(["git", "clone", "-q", "--depth", "1", GIT.format(gitlab_path(pkgbase)), path])
    return path


def download(url, dest):
    """makepkg's download agent (curl, with its retries: timeouts, 429, 5xx)."""
    if os.path.exists(dest):
        return dest
    run(["curl", "-qgb", "", "-fLsS", "--retry", "4", "--retry-delay", "10", "--connect-timeout", "60",
         "--speed-limit", "1", "--speed-time", "300", "-o", dest + ".part", url])
    os.replace(dest + ".part", dest)
    return dest


# Arch's source packages (makepkg --allsource of each release): every source
# as Arch built it, for upstream files since changed (GitHub, cgit and Forgejo
# patches and archives made again), gone (404) or on hosts that are down
SRCPKG = "https://sources.archlinux.org/sources/{}/{}.src.tar.gz"
_srcpkgs = {}


def arch_srcpkg(info, sources_dir):
    """This release's source package from sources.archlinux.org, or None."""
    base = info["pkgbase"] or info["pkgname"][0]
    ver = f"{info['pkgver']}-{info['pkgrel']}"
    names = ([f"{base}-{info['epoch']}:{ver}"] if info["epoch"] else []) + [f"{base}-{ver}"]
    key = names[-1]
    if key not in _srcpkgs:
        _srcpkgs[key] = None
        d = os.path.join(sources_dir, "arch-src")
        os.makedirs(d, exist_ok=True)
        for repo in ("extra", "core", "multilib"):
            for n in names:
                try:
                    _srcpkgs[key] = download(SRCPKG.format(repo, n), os.path.join(d, n + ".src.tar.gz"))
                    break
                except subprocess.CalledProcessError:
                    pass
            if _srcpkgs[key]:
                break
    return _srcpkgs[key]


def from_srcpkg(info, sources_dir, member, dest):
    """Take <pkgbase>/<member> (a file, or a VCS source's bare repository)
    out of Arch's source package to dest; dest, or None."""
    pkg = arch_srcpkg(info, sources_dir)
    if not pkg:
        return None
    base = info["pkgbase"] or info["pkgname"][0]
    name = f"{base}/{member}"
    with tarfile.open(pkg) as t:
        found = [m for m in t.getmembers() if m.name == name or m.name.startswith(name + "/")]
        if not found:
            return None
        tmp = dest + ".srcpkg"
        shutil.rmtree(tmp, ignore_errors=True)
        t.extractall(tmp, members=found, filter="data")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.replace(os.path.join(tmp, name), dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest


def mirrors():
    """arch/mirrors: URL prefix, and where the same repositories or files are."""
    out = []
    path = os.path.join(HERE, "mirrors")
    if os.path.exists(path):
        for l in open(path):
            f = l.split("#")[0].split()
            if len(f) == 2:
                out.append((f[0], f[1]))
    return out


def checksum(path, algo):
    h = {"sha256": hashlib.sha256, "sha512": hashlib.sha512, "b2": lambda: hashlib.blake2b(digest_size=64)}[algo]()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def wrong_sum(path, sums, i):
    """Why the file is not the PKGBUILD's i-th source, or None."""
    for algo, vals in sums.items():
        if vals[i] != "SKIP" and checksum(path, algo) != vals[i]:
            with open(path, "rb") as f:
                head = f.read(512).lstrip().lower()
            page = ", a web page" if head.startswith((b"<!doctype html", b"<html")) else ""
            return f"{algo} does not match the PKGBUILD ({os.path.getsize(path)} bytes{page})"
    return None


def url_repo_name(src):
    """The directory a Git source is cloned into: name:: or the repo's name."""
    if "::" in src:
        return src.split("::", 1)[0]
    u = re.sub(r"^git\+", "", src).split("#")[0].split("?")[0].rstrip("/")
    return u.rsplit("/", 1)[-1]


def git_mirror(base, mirror, dirname, info, sources_dir):
    """A bare clone of base at mirror. A host that is down or limits its
    clients (dev.gnupg.org's 429) is tried again, then the same repository
    elsewhere (arch/mirrors), then the clone in Arch's source package."""
    err = None
    for wait in (0, 20, 60):
        time.sleep(wait)
        r = run(["git", "clone", "-q", "--mirror", base, mirror], check=False)
        if r.returncode == 0:
            return
        err = r
        shutil.rmtree(mirror, ignore_errors=True)
    for prefix, alt in mirrors():
        if base.startswith(prefix):
            r = run(["git", "clone", "-q", "--mirror", alt + base[len(prefix):], mirror], check=False)
            if r.returncode == 0:
                return
            shutil.rmtree(mirror, ignore_errors=True)
    if info and from_srcpkg(info, sources_dir, dirname, mirror) and os.path.isdir(mirror):
        return
    raise subprocess.CalledProcessError(err.returncode, err.args, err.stdout, err.stderr)


def git_snapshot(src, dirname, sources_dir, info=None):
    """A Git source as makepkg would check it out ($srcdir/<dirname>, with
    its .git: PKGBUILDs cherry-pick, apply and describe), packed in a tarball
    Yone unpacks. Cloned on this host: the build system has no network."""
    u = re.sub(r"^git\+", "", src.split("::", 1)[-1])
    # "#tag=v1.2?signed": the signature check is the packager's, not a ref
    frag = (u.split("#", 1)[1] if "#" in u else "").split("?", 1)[0]
    base = u.split("#")[0].split("?")[0]
    kind, _, ref = frag.partition("=")
    mirror = os.path.join(sources_dir, "git", re.sub(r"[^A-Za-z0-9._-]", "_", base))
    if os.path.isdir(mirror):
        # (a host down: the mirror as it is, which has the tag or commit)
        run(["git", "-C", mirror, "fetch", "-q", "--tags", "--force", "origin", "+refs/heads/*:refs/heads/*"], check=False)
    else:
        git_mirror(base, mirror, dirname, info, sources_dir)
    tag = re.sub(r"[^A-Za-z0-9._-]", "_", f"{kind}-{ref}" if ref else "HEAD")
    out = os.path.join(sources_dir, f"{dirname}-git-{tag}.tar.gz")
    out = out.replace(".tar.gz", "-sm.tar.gz")  # snapshots with their submodules
    if os.path.exists(out) and ref and kind in ("tag", "commit"):
        return out  # a tag or commit does not move
    work = out + ".d"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    co = os.path.join(work, dirname)
    run(["git", "clone", "-q", "--no-checkout", mirror, co])
    run(["git", "-C", co, "remote", "set-url", "origin", base])
    target = {"tag": f"refs/tags/{ref}", "commit": ref, "branch": f"origin/{ref}"}.get(kind, "HEAD")
    # makepkg checks a tag out by its value: a tag name, or the hash of the
    # tag object (#tag=26254ee9...), which no refs/tags/ name matches
    if kind == "tag" and run(["git", "-C", co, "rev-parse", "-q", "--verify", f"refs/tags/{ref}"], check=False).returncode:
        target = ref
    run(["git", "-C", co, "-c", "advice.detachedHead=false", "checkout", "-q", target])
    if kind == "branch":
        run(["git", "-C", co, "checkout", "-q", "-B", ref])
    # submodules fetched here, at the commits the checkout names: a
    # PKGBUILD's "git submodule update" in the build system (no network)
    # then finds them in place
    if os.path.exists(os.path.join(co, ".gitmodules")):
        run(["git", "-C", co, "submodule", "update", "--init", "--recursive", "-q"])
    run(["tar", "-C", work, "--sort=name", "--owner=0", "--group=0", "--numeric-owner",
         "-czf", out + ".part", dirname])
    os.replace(out + ".part", out)
    shutil.rmtree(work, ignore_errors=True)
    return out


def convert(pkgbase, cache, out, sources_dir, nmap, no_download=False):
    repo = fetch_repo(pkgbase, cache)
    info = json.loads(run(["bash", "-c", EVAL, "-", repo]).stdout)
    if info["arch"] and not set(info["arch"]) & {"x86_64", "any"}:
        return f"skipped: arch {info['arch']}"
    srcs = info["source"] + info["source_x86_64"]
    sums = {}
    for algo, keys in [("sha256", ["sha256sums", "sha256sums_x86_64"]), ("sha512", ["sha512sums", "sha512sums_x86_64"]),
                       ("b2", ["b2sums", "b2sums_x86_64"])]:
        v = info[keys[0]] + info[keys[1]]
        if len(v) == len(srcs):
            sums[algo] = v
    lines_src, local, gitdirs = [], [], []
    for i, s in enumerate(srcs):
        name, _, url = s.rpartition("::") if "::" in s else ("", "", s)
        if re.match(r"^(svn|hg|bzr)(\+|://)", url):
            return f"skipped: VCS source {url}"
        if re.match(r"^git(\+|://)", url):
            # makepkg: the name up to the first ".git" (notofonts.github.io: notofonts)
            dirname = name or re.sub(r"\.git.*$", "", url_repo_name(srcs[i]))
            gitdirs.append(dirname)
            if no_download:
                fname, sha = f"{dirname}-git.tar.gz", "TODO"
            else:
                path = git_snapshot(s, dirname, sources_dir, info)
                fname, sha = os.path.basename(path), checksum(path, "sha256")
            lines_src.append((f"{fname}::{url}", sha))
            continue
        else:
            fname = name or url.rsplit("/", 1)[-1]
        if "://" not in url:  # a file of the packaging repository
            local.append(url)
            sha = checksum(os.path.join(repo, url), "sha256")
            lines_src.append((url, sha))
            continue
        if url.endswith((".sig", ".asc", ".sign")):
            continue  # signatures: the checksums pin the files
        if no_download:
            known = sums.get("sha256", [None] * len(srcs))[i]
            sha = known if known and known != "SKIP" else "TODO"
        else:
            path = os.path.join(sources_dir, fname)
            try:
                if os.path.exists(path) and wrong_sum(path, sums, i):
                    os.remove(path)  # another base's file of that name
                err = wrong_sum(download(url, path), sums, i)
            except subprocess.CalledProcessError as e:
                err = ((e.stderr or "").strip().splitlines() or [str(e)])[-1]
            if err:
                # what Arch built with: its source package (the sums still checked)
                got = from_srcpkg(info, sources_dir, fname, path + ".arch")
                why = (wrong_sum(path + ".arch", sums, i) if got else
                       "not in it" if arch_srcpkg(info, sources_dir) else
                       f"no {info['pkgver']}-{info['pkgrel']} on sources.archlinux.org")
                if why:
                    raise SystemExit(f"{pkgbase}: {fname}: {err} (nor in Arch's source package: {why})")
                os.replace(path + ".arch", path)
            sha = checksum(path, "sha256")
        lines_src.append((f"{fname}::{url}" if name else url, sha))
    funcs = run(["bash", "-c", 'cd "$1"; CARCH=x86_64; source ./PKGBUILD >/dev/null; declare -f', "-", repo]).stdout
    # the PKGBUILD's own variables (minus arrays already handled), for its functions
    pkgbuild = open(os.path.join(repo, "PKGBUILD")).read()
    gone = drops(pkgbase)
    # a package of the base itself that arch/drop names is not made (opencv's
    # opencv-cuda), nor run before the others as a split package
    names = [n for n in info["pkgname"] if n not in gone]
    version = info["pkgver"]
    made = []
    for name in names:
        d = os.path.join(out, name)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        for f in local:
            shutil.copy2(os.path.join(repo, f), os.path.join(d, f))
        deps = map_deps(info["depends"], nmap, names)
        pfunc = f"package_{name}" if f"package_{name}" in funcs else "package"
        # per-package depends declared inside package_<name>() are read at run time by makepkg;
        # take them from the function text
        m = re.search(rf"{re.escape(pfunc)} \(\)\s*\{{(.*?)\n\}}", funcs, re.S)
        if m:
            inner = re.search(r"\bdepends=\((.*?)\)", m.group(1), re.S)
            if inner:
                deps = map_deps(re.findall(r"[^\s'\"]+", inner.group(1)), nmap, names)
        mk = map_deps(info["makedepends"], nmap, names)
        deps = [d for d in deps if bare(d) not in gone]
        mk = [d for d in mk if bare(d) not in gone]
        y = [f"# Converted from Arch Linux's {pkgbase} (arch/import.py); PKGBUILD 0BSD.",
             f"name = {name}", f"version = {version}", f"release = {info['pkgrel']}",
             f"desc = {info['pkgdesc'].strip() or name}"]
        if info["url"]:
            y.append(f"url = {info['url']}")
        if info["license"]:
            y.append("license = " + " ".join(info["license"]))
        if deps:
            y.append("depends = " + " ".join(deps))
        if mk:
            y.append("makedepends = " + " ".join(mk))
        if info["backup"]:
            y.append("backup = " + " ".join(info["backup"]))
        for s, sha in lines_src:
            y += [f"source = {s}", f"sha256 = {sha}"]
        # makepkg passes over a noextract name that is no source's
        # (archlinux-appstream-data's core.xml.gz-<ver>); Yone refuses it
        srcnames = {s.split("::", 1)[0] if "::" in s else s.rsplit("/", 1)[-1] for s, _ in lines_src}
        noextract = [n for n in info["noextract"] if n in srcnames]
        if noextract:
            y.append("noextract = " + " ".join(noextract))
        body = [
            "@package",
            "# --- makepkg's environment",
            FLAGS,
            # makepkg: srcdir is $startdir/src, and the PKGBUILD's own files
            # are in $startdir too (patches read as ../file)
            'srcdir="$SRCDIR"; pkgdir="$PKGDIR"; startdir="${SRCDIR%/*}"; CARCH=x86_64',
            *[f'cp -n "$srcdir"/{shq(f)} "$startdir"/' for f in local],
            # the Git snapshots' index, written on another machine, out of
            # date once unpacked: git apply --index and cherry-pick refuse it
            *[f'git -C "$srcdir"/{shq(g)} update-index -q --refresh > /dev/null 2>&1 || true' for g in gitdirs],
            "# --- the PKGBUILD, as Arch publishes it",
            "# makepkg runs it with errexit but without pipefail: a pipeline's",
            "# status is its last command's (waf's head | tail | sed)",
            "set +u +o pipefail; shopt -s extglob  # makepkg has extglob on",
            pkgbuild.replace("\r", ""),
            "# makepkg: pkgbase is the first name when the PKGBUILD has none;",
            "# pkgname stays the PKGBUILD's list for prepare() and build()",
            'pkgbase=${pkgbase:-${pkgname[0]}}',
            "# makepkg: options=(!makeflags) builds without MAKEFLAGS (-jN): the",
            "# Makefiles of cdparanoia, ffcall and catdoc are not safe in parallel",
            'case " ${options[*]-} " in *" !makeflags "*) unset MAKEFLAGS ;; esac',
            *(["# --- GraveyardOS's fixes (arch/fixes)"] + fixes(pkgbase) if fixes(pkgbase) else []),
            "# --- what makepkg runs",
            "# prepare() once: a build resumed (YONE_RESUME) finds it done",
            'cd "$srcdir"; if [ ! -e .prepared ]; then if declare -F prepare >/dev/null; then prepare; fi; cd "$srcdir"; touch .prepared; fi',
            'cd "$srcdir"; if declare -F build >/dev/null; then build; fi',
            # split packages: makepkg runs every package_*() in turn in the
            # same $srcdir, and one may leave files there for a later one
            # (sqlite's moves its Tcl extension out for sqlite-tcl); the
            # ones before this package run first, into throwaway directories
            *[f'(pkgname={e}; pkgdir="$srcdir/.split/{e}"; mkdir -p "$pkgdir"; cd "$srcdir"; package_{e})'
              for e in names[:names.index(name)] if pfunc != "package" and f"package_{e}" in funcs],
            # makepkg sets pkgname to the package being made for its package_*()
            f'pkgname={name}; cd "$srcdir"; {pfunc}',
            "# --- what makepkg does after package() (makepkg.conf: strip docs !libtool",
            "# !staticlibs emptydirs zipman purge), as the PKGBUILD's options= allow",
            TIDY,
        ]
        open(os.path.join(d, "YONEBUILD"), "w").write("\n".join(y) + "\n\n" + "\n".join(body) + "\n")
        made.append(name)
    return "ok: " + " ".join(made)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/graveyard-arch/repos"))
    ap.add_argument("--sources", default=os.path.expanduser("~/.cache/graveyard-arch/sources"))
    ap.add_argument("--no-download", action="store_true",
                    help="write the recipes without fetching sources (SHA-256 left as TODO when unknown)")
    ap.add_argument("pkgbases", nargs="+")
    a = ap.parse_args()
    for d in (a.out, a.cache, a.sources):
        os.makedirs(d, exist_ok=True)
    nmap = names_map()
    bad = 0
    for p in a.pkgbases:
        try:
            r = convert(p, a.cache, a.out, a.sources, nmap, a.no_download)
        except (subprocess.CalledProcessError, OSError, SystemExit) as e:
            r = f"FAILED: {getattr(e, 'stderr', '') or e}".strip()
        if not r.startswith("ok"):
            bad += 1
        print(f"{p:28} {r}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
