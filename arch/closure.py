#!/usr/bin/env python3
"""What a set of Arch Linux packages needs: the dependency closure, from the
archlinux.org package API (runtime dependencies, and with --make the build
dependencies too), minus what the GraveyardOS base already has.

    python3 arch/closure.py [--make] [--cache DIR] [--base FILE] target ...

A target is a package name or @group (e.g. @plasma). Prints one package
base per line in build order (dependencies first), then a summary.

With --make, every package of a package base reached is taken in, not only
the one a dependency names: arch/import.py builds them all (the musepack
base makes libmpcdec and musepack-tools, which needs libcue), so a base
needs the depends of all its packages and its makedepends, and what they
reach joins the closure. Bases of the base system (--base) are not built
and are not expanded. A dependency on a GraveyardOS recipe of arch/extra
(minizip, which the base's zlib stands in for) is named too.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://archlinux.org/packages/search/json/?"
HERE = os.path.dirname(os.path.abspath(__file__))

def get(url, cache):
    key = os.path.join(cache, re.sub(r"[^A-Za-z0-9._-]", "_", url)[-200:])
    if os.path.exists(key):
        return open(key).read()
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "graveyardos-closure"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:  # no such page: nothing to read (cached as such)
                text = ""
                break
            time.sleep(2 ** attempt)
        except OSError:
            time.sleep(2 ** attempt)
    else:
        raise SystemExit(f"cannot fetch {url}")
    open(key, "w").write(text)
    return text

def bare(dep):
    return re.split(r"[<>=]", dep, maxsplit=1)[0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", action="store_true")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/graveyard-arch"))
    ap.add_argument("--base", help="file: Arch names the base provides, one per line")
    ap.add_argument("--graph", help="with --order: write 'base<TAB>bases it needs' to this file")
    ap.add_argument("--order", action="store_true",
                    help="print the package bases in build order (dependencies and build dependencies first)")
    ap.add_argument("--expand", action="store_true",
                    help="print the targets with their @groups expanded, one per line, and stop")
    ap.add_argument("targets", nargs="+")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    base = set()
    if a.base:
        base = {n for l in open(a.base) for n in l.split("#")[0].split()}

    by_name, provides = {}, {}
    # GraveyardOS's own recipes (arch/extra), by name
    extra_dir = os.path.join(HERE, "extra")
    extras = set(os.listdir(extra_dir)) if os.path.isdir(extra_dir) else set()

    def lookup(name):
        if name in by_name:
            return by_name[name]
        d = json.loads(get(API + urllib.parse.urlencode({"name": name, "arch": ["x86_64", "any"]}, doseq=True), a.cache))
        res = [r for r in d["results"] if r["repo"] in ("core", "extra")]
        by_name[name] = res[0] if res else None
        return by_name[name]

    def group(g):
        out, page = [], 1
        while True:
            d = json.loads(get(API + urllib.parse.urlencode({"q": "", "repo": ["Core", "Extra"], "arch": "x86_64", "page": page}, doseq=True) + f"&groups={g}", a.cache))
            out += [r["pkgname"] for r in d["results"] if g in r.get("groups", [])]
            if page >= d.get("num_pages", 1):
                return out
            page += 1

    def split(r):
        """The names of all the packages of r's package base, from its page
        on archlinux.org: the split package list when no package has the
        base's name, else that package and its "Split Packages"."""
        b = r["pkgbase"]
        html = get(f"https://archlinux.org/packages/{r['repo']}/{r['arch']}/{b}/", a.cache)
        if "Split Package Details" in html:
            names = re.findall(r'title="View package details for ([^"]+)"', html)
        else:
            m = re.search(r"<th>Split Packages:</th>(.*?)</tr>", html, re.S)
            names = re.findall(r'title="View package details for ([^"]+)"', m.group(1)) if m else []
            own = lookup(b)
            if own and own["pkgbase"] == b:
                names.append(b)
        return names

    todo = []
    for t in a.targets:
        todo += group(t[1:]) if t.startswith("@") else [t]
    if a.expand:
        print("\n".join(dict.fromkeys(todo)))
        return
    seen, unresolved, order, expanded = set(), set(), [], set()
    while todo:
        batch = [n for n in dict.fromkeys(todo) if n not in seen and n not in base]
        todo = []
        seen.update(batch)
        with ThreadPoolExecutor(8) as ex:
            found = list(ex.map(lookup, batch))
        grown = []
        for n, r in zip(batch, found):
            if not r:
                unresolved.add(n)
                continue
            order.append(r)
            deps = r["depends"] + (r["makedepends"] if a.make else [])
            todo += [bare(x) for x in deps if bare(x) not in base]
            # with --make: the base's other packages too (import.py builds them all)
            if a.make and r["pkgbase"] not in base and r["pkgbase"] not in expanded:
                expanded.add(r["pkgbase"])
                grown.append(r)
        with ThreadPoolExecutor(8) as ex:
            for names in ex.map(split, grown):
                todo += names
    bases = {}
    for r in order:
        bases.setdefault(r["pkgbase"], []).append(r["pkgname"])
    if a.order:
        # base -> bases it needs (runtime and build), within the set
        base_of = {r["pkgname"]: r["pkgbase"] for r in order}
        for r in order:
            provided = [bare(p) for p in r.get("provides", [])]
            # what stands in for a package of the base system (zlib-ng-compat
            # for zlib): its names, libz.so too, stay the base's
            if any(p in base for p in provided):
                continue
            for p in provided:
                base_of.setdefault(p, r["pkgbase"])
        need = {b: set() for b in bases}
        for r in order:
            for d in r["depends"] + r["makedepends"]:
                nb = base_of.get(bare(d))
                if nb and nb != r["pkgbase"]:
                    need[r["pkgbase"]].add(nb)
                if bare(d) in extras:  # built from arch/extra, not from its Arch base
                    need[r["pkgbase"]].add(bare(d))
        done, out, active = set(), [], set()
        def visit(b):
            if b in done or b in active:  # a cycle: the book of the world breaks it by order
                return
            active.add(b)
            for n in sorted(need[b]):
                if n in need:  # not an arch/extra recipe
                    visit(n)
            active.discard(b)
            done.add(b)
            out.append(b)
        for b in sorted(bases):
            visit(b)
        for b in out:
            print(b)
        if a.graph:
            with open(a.graph, "w") as g:
                for b in out:
                    g.write(b + "\t" + " ".join(sorted(need[b])) + "\n")
    else:
        for b, names in sorted(bases.items()):
            print(b + ("" if names == [b] else "\t" + " ".join(sorted(names))))
    size = sum(r["installed_size"] for r in order)
    print(f"# {len(order)} packages from {len(bases)} package bases, {size/2**30:.1f} GiB installed; "
          f"{len(unresolved)} names not found as packages (virtual: provided by another): "
          + " ".join(sorted(unresolved))[:600], file=sys.stderr)

if __name__ == "__main__":
    main()
