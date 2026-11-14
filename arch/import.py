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
packages, not libraries. A package the importer cannot convert (a VCS source,
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
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GIT = "https://gitlab.archlinux.org/archlinux/packaging/packages/{}.git"

# makepkg.conf (Arch, x86_64), the flags every package is built with
FLAGS = r'''export CHOST=x86_64-pc-linux-gnu
export CFLAGS="-march=x86-64 -mtune=generic -O2 -pipe -fno-plt -fexceptions -Wp,-D_FORTIFY_SOURCE=3 -Wformat -Werror=format-security -fstack-clash-protection -fcf-protection -fno-omit-frame-pointer -mno-omit-leaf-frame-pointer"
export CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
export LDFLAGS="-Wl,-O1 -Wl,--sort-common -Wl,--as-needed -Wl,-z,relro -Wl,-z,now -Wl,-z,pack-relative-relocs"
export LTOFLAGS="-flto=auto"
export RUSTFLAGS="-Cforce-frame-pointers=yes"
export DEBUG_CFLAGS="-g" DEBUG_CXXFLAGS="-g"'''

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

SONAME = re.compile(r"\.so(=|$)")


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


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


def fetch_repo(pkgbase, cache):
    path = os.path.join(cache, pkgbase)
    if os.path.isdir(os.path.join(path, ".git")):
        return path
    run(["git", "clone", "-q", "--depth", "1", GIT.format(pkgbase), path])
    return path


def download(url, dest):
    if os.path.exists(dest):
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "graveyardos-import"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest + ".part", "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(dest + ".part", dest)
    return dest


def checksum(path, algo):
    h = {"sha256": hashlib.sha256, "sha512": hashlib.sha512, "b2": lambda: hashlib.blake2b(digest_size=64)}[algo]()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def url_repo_name(src):
    """The directory a Git source is cloned into: name:: or the repo's name."""
    if "::" in src:
        return src.split("::", 1)[0]
    u = re.sub(r"^git\+", "", src).split("#")[0].split("?")[0].rstrip("/")
    return u.rsplit("/", 1)[-1]


def vcs_archive(src):
    """git+https://host/owner/repo.git#tag=X -> (archive URL, top directory)."""
    u = src.split("::", 1)[-1]
    u = re.sub(r"^git\+", "", u)
    frag = u.split("#", 1)[1] if "#" in u else ""
    base = u.split("#")[0].split("?")[0].rstrip("/")
    base = re.sub(r"\.git$", "", base)
    m = re.match(r"(tag|commit)=(.+)", frag)
    if not m:
        return None
    ref = m.group(2)
    repo = base.rsplit("/", 1)[-1]
    if base.startswith("https://github.com/"):
        return f"{base}/archive/{ref}.tar.gz", f"{repo}-{ref.lstrip('v') if m.group(1) == 'tag' else ref}"
    if re.match(r"https://(gitlab\.[^/]+|invent\.kde\.org|gitlab\.freedesktop\.org|gitlab\.gnome\.org)/", base):
        return f"{base}/-/archive/{ref}/{repo}-{ref}.tar.gz", f"{repo}-{ref}"
    return None


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
    lines_src, local, renames = [], [], []
    for i, s in enumerate(srcs):
        name, _, url = s.rpartition("::") if "::" in s else ("", "", s)
        rename = None
        if re.match(r"^(svn|hg|bzr)(\+|://)", url):
            return f"skipped: VCS source {url}"
        if re.match(r"^git(\+|://)", url):
            # A tag or commit of a Git repository: the host's archive of it,
            # unpacked under the name the clone would have had.
            arch = vcs_archive(url)
            if not arch:
                return f"skipped: Git source without an archive URL: {url}"
            url, top = arch
            rename = (top, name or re.sub(r"\.git$", "", url_repo_name(srcs[i])))
            fname = rename[1] + "-" + os.path.basename(url)
            name = fname
        else:
            fname = name or url.rsplit("/", 1)[-1]
        if "://" not in url:  # a file of the packaging repository
            local.append(url)
            sha = checksum(os.path.join(repo, url), "sha256")
            lines_src.append((url, sha))
            continue
        if url.endswith((".sig", ".asc", ".sign")):
            continue  # signatures: the checksums pin the files
        if rename:
            renames.append(rename)
        if no_download:
            known = sums.get("sha256", [None] * len(srcs))[i]
            sha = known if known and known != "SKIP" and not rename else "TODO"
        else:
            path = download(url, os.path.join(sources_dir, fname))
            for algo, vals in sums.items():
                if not rename and vals[i] != "SKIP" and checksum(path, algo) != vals[i]:
                    raise SystemExit(f"{pkgbase}: {fname}: {algo} does not match the PKGBUILD")
            sha = checksum(path, "sha256")
        lines_src.append((f"{fname}::{url}" if name else url, sha))
    funcs = run(["bash", "-c", 'cd "$1"; CARCH=x86_64; source ./PKGBUILD >/dev/null; declare -f', "-", repo]).stdout
    # the PKGBUILD's own variables (minus arrays already handled), for its functions
    pkgbuild = open(os.path.join(repo, "PKGBUILD")).read()
    names = info["pkgname"]
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
        if info["noextract"]:
            y.append("noextract = " + " ".join(info["noextract"]))
        body = [
            "@package",
            "# --- makepkg's environment",
            FLAGS,
            'srcdir="$SRCDIR"; pkgdir="$PKGDIR"; startdir="$SRCDIR"; CARCH=x86_64',
            "# --- the PKGBUILD, as Arch publishes it",
            "set +u",
            pkgbuild.replace("\r", ""),
            f"pkgname={name}",
            "# --- what makepkg runs",
            *[f'cd "$srcdir"; mv {shq(t)} {shq(n)}  # a Git tag, as its archive unpacks' for t, n in renames],
            'cd "$srcdir"; if declare -F prepare >/dev/null; then prepare; fi',
            'cd "$srcdir"; if declare -F build >/dev/null; then build; fi',
            f'cd "$srcdir"; {pfunc}',
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
