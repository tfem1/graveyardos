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
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def lines(path):
    try:
        return [l.split("\t")[0].strip() for l in open(path) if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def main():
    work, shards, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    order = lines(os.path.join(HERE, "order"))
    base = {n for l in open(os.path.join(HERE, "base-names")) for n in l.split("#")[0].split()}
    needs = {}
    for l in open(os.path.join(HERE, "needs")):
        if "\t" in l:
            b, deps = l.rstrip("\n").split("\t", 1)
            needs[b] = deps.split()
    done = set(lines(os.path.join(work, "done"))) | base
    failed = set() if "--retry-failed" in sys.argv else set(lines(os.path.join(work, "failed")))
    ready = [b for b in order if b not in done and b not in failed
             and all(d in done for d in needs.get(b, []))]
    blocked = [b for b in order if b not in done and b not in failed and b not in ready]
    n = max(1, min(shards, len(ready)))
    os.makedirs(out, exist_ok=True)
    for i in range(n):
        with open(os.path.join(out, f"shard-{i}"), "w") as f:
            f.write("".join(b + "\n" for b in ready[i::n] + blocked[i::n]))
    print(json.dumps({"shard": list(range(n)) if ready else []}))
    print(f"done {len(done & set(order))}, failed {len(failed)}, ready {len(ready)} in {n if ready else 0} shards, "
          f"waiting for dependencies {len(blocked)}, of {len(order)}", file=sys.stderr)


if __name__ == "__main__":
    main()
