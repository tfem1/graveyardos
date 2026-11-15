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
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    work = sys.argv[1]
    path = os.path.join(work, "done")
    if not os.path.exists(path):
        return
    done = [l.rstrip("\n") for l in open(path) if l.strip()]
    order = {l.split("\t")[0].strip() for l in open(os.path.join(HERE, "order")) if l.strip() and not l.startswith("#")}
    keep = set()
    for y in glob.glob(os.path.join(HERE, "bootstrap", "*", "YONEBUILD")):
        for l in open(y):
            if l.startswith("# replaces:"):
                keep.update(l.split(":", 1)[1].split())
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
