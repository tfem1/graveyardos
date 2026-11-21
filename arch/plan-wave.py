#!/usr/bin/env python3
"""The package bases a parallel wave can build now, split into shards.

    python3 arch/plan-wave.py WORK SHARDS OUTDIR [--retry-failed]

WORK holds the state of the last pass (done, failed). A base is ready when
it is not done, did not fail (unless --retry-failed: after fixes), and every
base it needs (arch/needs) is done.
The ready ones, in build order (arch/order), are dealt round-robin into at
most SHARDS lists OUTDIR/shard-<n>, then the rest (those waiting for
dependencies) the same way after them: a shard goes on with what its own
builds make ready (build-wave.sh skips what still waits). Prints the matrix
as JSON for GitHub Actions ({"shard": [0, 1, ...]}), a summary on stderr.
"""
import json
import re
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def lines(path):
    try:
        return [l.split("\t")[0].strip() for l in open(path) if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def sccs(nodes, needs):
    """The strongly connected components of the needs graph among nodes
    (Tarjan's algorithm, iterative)."""
    nodes = list(nodes)
    inside = set(nodes)
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter([d for d in needs.get(root, []) if d in inside]))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            v, it = work[-1]
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1
                    stack.append(w); on.add(w)
                    work.append((w, iter([d for d in needs.get(w, []) if d in inside])))
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop(); on.discard(w); comp.append(w)
                        if w == v:
                            break
                    out.append(comp)
    return out


# Dependencies that close most cycles and that a build can do without for a
# first round: documentation and test tools. A cycle is broken at them first.
SOFT = re.compile(r"^(python-sphinx.*|sphinx.*|doxygen|graphviz|gi-docgen|gtk-doc|asciidoc|asciidoctor|xmlto|docbook-.*|"
                  r"help2man|texlive-.*|texinfo|python-docutils|pandoc.*|ruby-ronn|python-myst-parser|python-breathe|"
                  r"python-pytest.*|check|cmocka|gtest|catch2|doctest|valgrind|python-hypothesis|python-mock|"
                  r"python-sphinx_rtd_theme|python-furo|python-recommonmark|mkdocs.*|python-mkdocs.*|ghostscript|"
                  r"dblatex|w3m|lynx|man-db|groff|yelp-tools|itstool|python-babel|python-pygments)$")


def els_order(comp, needs, pinned=frozenset()):
    """Eades-Lin-Smyth: an order of a cycle's bases with few back edges
    (a base after the bases it needs, as far as the cycle allows)."""
    cs = set(comp)
    deps = {b: {d for d in needs.get(b, []) if d in cs and d != b} for b in comp}
    users = {b: set() for b in comp}
    for b in comp:
        for d in deps[b]:
            users[d].add(b)
    left = set(comp)
    front, back = [], []
    key = {b: i for i, b in enumerate(comp)}
    while left:
        moved = True
        while moved:
            moved = False
            for b in sorted(left, key=key.get):
                if not (deps[b] & left):          # needs nothing left: early
                    front.append(b); left.discard(b); moved = True
            for b in sorted(left, key=key.get):
                if not (users[b] & left):         # nothing left needs it: late
                    back.insert(0, b); left.discard(b); moved = True
        if left:
            # a break point; a base that failed as one (pinned) is not chosen again
            pool = (left - pinned) or left
            b = max(sorted(pool, key=key.get), key=lambda x: len(users[x] & left) - len(deps[x] & left))
            front.append(b); left.discard(b)
    return front + back


def main():
    work, shards, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    long = set(lines(os.path.join(HERE, "long")))
    order = [b for b in lines(os.path.join(HERE, "order")) if b not in long]
    orderset = set(order)
    extras = set(os.listdir(os.path.join(HERE, "extra")))
    base = {n for l in open(os.path.join(HERE, "base-names")) for n in l.split("#")[0].split()}
    needs = {}
    for l in open(os.path.join(HERE, "needs")):
        if "\t" in l:
            b, deps = l.rstrip("\n").split("\t", 1)
            needs[b] = deps.split()
    for l in open(os.path.join(HERE, "drop")):
        f = l.rstrip("\n").split("\t")
        if len(f) >= 2 and not l.startswith("#") and f[0] in needs:
            needs[f[0]] = [d for d in needs[f[0]] if d != f[1]]

    # Dependency cycles, broken once: in each cycle (strongly connected
    # component), a base does not wait for the cycle's documentation and test
    # tools, nor for the bases after it in an order with few back edges
    # (Eades-Lin-Smyth, on the other dependencies): as Arch builds a cycle on
    # its previous packages. "relax" is what a base is built without.
    done = set(lines(os.path.join(work, "done"))) | base
    # A base done (built, or replaced by a bootstrap: cmake, git) waits for
    # nothing more: what it needs closes no cycle
    needs = {b: ([] if b in done else ds) for b, ds in needs.items()}
    # a pin holds while the base's arch/fixes lines stay as they were when it
    # failed (build-wave.sh's fixes_print): a new fix lifts it
    import hashlib
    fixlines = {}
    for l in open(os.path.join(HERE, "fixes")):
        fixlines.setdefault(l.split("\t", 1)[0], []).append(l if l.endswith("\n") else l + "\n")
    def fprint(b):
        return hashlib.md5("".join(fixlines.get(b, [])).encode()).hexdigest()[:8]
    failed = {}
    if os.path.exists(os.path.join(work, "failed")):
        for l in open(os.path.join(work, "failed")):
            f = l.rstrip("\n").split("\t")
            if f[0]:
                m = f[2] if len(f) > 2 else ""
                failed[f[0]] = m == "relaxed:" + fprint(f[0]) or (m == "relaxed" and not fixlines.get(f[0]))
    # A base that failed when built without part of its cycle cannot do
    # without it: it is pinned (it waits for all it needs, and the cycle is
    # broken at another base), and planned again
    pinned = {b for b, relaxed in failed.items() if relaxed}
    for b in pinned:
        del failed[b]
    # tried again (fixes pushed): every failure is planned, the pins hold
    if "--retry-failed" in sys.argv:
        failed = {}

    hard = {b: [d for d in ds if not SOFT.match(d)] for b, ds in needs.items()}
    relax = {}
    for comp in sccs(order, needs):
        if len(comp) < 2:
            continue
        cs = set(comp)
        pos = {b: i for i, b in enumerate(els_order(sorted(comp, key=order.index), hard, pinned))}
        for b in comp:
            if b in pinned:
                continue
            r = [d for d in needs.get(b, []) if d in cs and (SOFT.match(d) or pos[d] > pos[b])]
            if r:
                relax[b] = r
    eff = {b: [d for d in needs.get(b, []) if d not in relax.get(b, ())] for b in order}
    # Last resort: a cycle left (its bases all pinned) is broken all the same,
    # at its bases' later dependencies in the order with few back edges
    while True:
        rest = [c for c in sccs(order, eff) if len(c) > 1]
        if not rest:
            break
        for comp in rest:
            cs = set(comp)
            pos = {b: i for i, b in enumerate(els_order(sorted(comp, key=order.index), eff))}
            for b in comp:
                late = [d for d in eff[b] if d in cs and pos[d] > pos[b]]
                if late:
                    relax[b] = relax.get(b, []) + late
                    eff[b] = [d for d in eff[b] if d not in late]

    # Shards: in an order where each base comes after what it waits for, a
    # base joins the shard that builds its unbuilt dependencies when they are
    # all in one, so a shard climbs a chain within one wave; otherwise the
    # least loaded shard
    import graphlib
    topo = [b for b in graphlib.TopologicalSorter(
        {b: [d for d in eff[b] if d in orderset] for b in order}).static_order() if b in orderset]
    # built without part of their cycle, which is there now: built again
    again = set()
    if os.path.exists(os.path.join(work, "partial")):
        for l in open(os.path.join(work, "partial")):
            f = l.rstrip("\n").split("\t")
            if len(f) == 2 and f[0] in done and f[1] != "-" and all(d in done for d in f[1].split()):
                again.add(f[0])
            elif len(f) == 2:
                again.discard(f[0])
    # arch/rebuild: a base built before a fix that changes what it makes
    # (base<TAB>number<TAB>why), built again once per number
    for l in open(os.path.join(HERE, "rebuild")):
        f = l.rstrip("\n").split("\t")
        if len(f) >= 2 and not l.startswith("#") and f[0] in done and f"rebuilt:{f[0]}@{f[1]}" not in done:
            again.add(f[0])
    pending = [b for b in topo if (b not in done or b in again) and b not in failed]
    ready = [b for b in pending if all(d in done for d in eff[b])]
    shard_of, lists = {}, [[] for _ in range(max(1, shards))]
    for b in pending:
        homes = {shard_of[d] for d in eff[b] if d in shard_of}
        k = homes.pop() if len(homes) == 1 else min(range(len(lists)), key=lambda i: len(lists[i]))
        shard_of[b] = k
        lists[k].append(b)
    lists = [l for l in lists if l]
    os.makedirs(out, exist_ok=True)
    for i, l in enumerate(lists):
        with open(os.path.join(out, f"shard-{i}"), "w") as f:
            f.write("".join(("+" if b in again else "") + b + ("\t" + " ".join(relax[b]) if b in relax else "") + "\n" for b in l))
    n = len(lists) if ready else 0

    # What holds the waiting bases back: their dependencies that cannot be
    # built now (failed, set aside in arch/long, or named nowhere), by how
    # many waiting bases need them, directly or through others
    blocked = [b for b in pending if b not in ready]
    stuck = set(failed) | long | {d for b in blocked for d in eff[b]
                                  if d not in done and d not in orderset and d not in extras}
    count = {}
    for b in blocked:
        seen, todo = set(), [b]
        while todo:
            x = todo.pop()
            for d in eff.get(x, []):
                if d in done or d in seen:
                    continue
                seen.add(d)
                if d not in stuck:
                    todo.append(d)
        for d in seen & stuck:
            count[d] = count.get(d, 0) + 1
    top = sorted(count.items(), key=lambda kv: -kv[1])[:25]
    print("held back by: " + ", ".join(f"{d} ({c})" for d, c in top), file=sys.stderr)
    print(f"cycles: {len(relax)} bases built without part of their cycle; pinned after failing so: "
          f"{len(pinned)}" + (f" ({' '.join(sorted(pinned))})" if pinned else ""), file=sys.stderr)
    print(json.dumps({"shard": list(range(n))}))
    print(f"built again now that their cycle is: {len(again)}" + (f" ({' '.join(sorted(again))})" if again else ""), file=sys.stderr)
    print(f"done {len(done & orderset)}, failed {len(failed)}, ready now {len(ready)}, "
          f"planned {len(pending)} in {n} shards, of {len(order)}", file=sys.stderr)


if __name__ == "__main__":
    main()
