#!/usr/bin/env python3
"""A package base counts as built (the work's done file) only while its
packages are in the repository: earlier passes took out of it what could not
be installed yet, and left the base in done, so it was never built again
(libgpg-error, libunistring...). Such bases leave done here.

    python3 arch/repair-done.py WORK

A base is in the repository when a package is named after it or starts with
"<base>-" (split packages: hyphen-en). Bases a bootstrap replaces, the base
system's names and GraveyardOS's extras are left alone.
"""
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


INFO_DIR = "usr/share/info/dir"
RULES = 2  # info dir, perllocal.pod/.packlist, usr/lib64


def listing(f):
    """The paths a package installs: its manifest, else the archive's list."""
    r = subprocess.run(["tar", "-I", "zstd", "-xOf", f, "--occurrence=1", ".YONEFILES"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout:
        return {p for l in r.stdout.splitlines() for p in l.split("\t")}  # the path is one of the fields
    r = subprocess.run(["tar", "-I", "zstd", "-tf", f], capture_output=True, text=True)
    return {l.rstrip("/") for l in r.stdout.splitlines()}


def main():
    work = sys.argv[1]
    path = os.path.join(work, "done")
    if not os.path.exists(path):
        return
    done = [l.rstrip("\n") for l in open(path) if l.strip()]
    order = {l.split("\t")[0].strip() for l in open(os.path.join(HERE, "order")) if l.strip() and not l.startswith("#")}
    keep = {n for l in open(os.path.join(HERE, "base-names")) for n in l.split("#")[0].split()}
    for y in glob.glob(os.path.join(HERE, "bootstrap", "*", "YONEBUILD")):
        for l in open(y):
            if l.startswith("# replaces:"):
                keep.update(l.split(":", 1)[1].split())
    # Packages built before the makepkg tidy step carry usr/share/info/dir
    # (Perl's: perllocal.pod, .packlist), or built in /usr/lib64 before the
    # build system said it was Arch (CMake, meson: lib64, out of every path):
    # the second one to be installed conflicts with the first (libgpg-error,
    # libunistring, nettle... out of every system since). Out of the
    # repository: their bases are built again, tidied.
    # the cache of clean files holds for one set of rules: a new rule checks them all again
    checked_path = os.path.join(work, f"repo-checked-{RULES}")
    checked = set(open(checked_path).read().split()) if os.path.exists(checked_path) else set()
    dropped = []
    for f in sorted(glob.glob(os.path.join(work, "repo", "*.yon"))):
        b = os.path.basename(f)
        if b in checked:
            continue
        if any(p == INFO_DIR or p.endswith("/perllocal.pod") or p.endswith("/.packlist")
               or p.startswith("usr/lib64/") for p in listing(f)):
            os.remove(f)
            if os.path.exists(f + ".sig"):
                os.remove(f + ".sig")
            dropped.append(b)
        else:
            checked.add(b)
    with open(checked_path, "w") as out:
        out.write("".join(c + "\n" for c in sorted(checked)))
    if dropped:
        print(f"with usr/share/info/dir, perllocal.pod or usr/lib64, out of the repository: {len(dropped)}: {' '.join(dropped)}")
        # a bootstrap among them is built again too
        boots = {os.path.basename(os.path.dirname(y)) for y in glob.glob(os.path.join(HERE, "bootstrap", "*", "YONEBUILD"))}
        again = {"bootstrap:" + re.sub(r"-[^-]+-[^-]+-[^-]+\.yon$", "", d) for d in dropped} & {"bootstrap:" + b for b in boots}
        if again:
            done = [b for b in done if b not in again]
            with open(path + ".new", "w") as f:
                f.write("".join(b + "\n" for b in done))
            os.replace(path + ".new", path)
    # name-version-release-arch.yon
    names = {re.sub(r"-[^-]+-[^-]+-[^-]+\.yon$", "", os.path.basename(f))
             for f in glob.glob(os.path.join(work, "repo", "*.yon"))}
    gone = []
    for b in done:
        if b not in order or b in keep:
            continue
        if b in names or any(n.startswith(b + "-") for n in names):
            continue
        gone.append(b)
    if gone:
        g = set(gone)
        with open(path + ".new", "w") as f:
            f.write("".join(b + "\n" for b in done if b not in g))
        os.replace(path + ".new", path)
        print(f"not in the repository, built again: {len(gone)}: {' '.join(sorted(gone))}")


if __name__ == "__main__":
    main()
