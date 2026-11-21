"""Turn raw coverage JSON into a compact map used by the selector.

map.json.gz structure:
  tests: [nodeid...] in tests.json order
  sets: list of sorted test-number lists (deduplicated)
  lines: {file: {line: set_index}}       per-test runtime coverage
  importlines: {file: [lines]}           lines executed at import time
  opened: {path: [test numbers]}         files/dirs read by tests
"""
import gzip
import json
import sys
from collections import defaultdict

cov_path, imp_path, tests_path, out_path = sys.argv[1:5]
raw = json.load(open(cov_path))
imp = json.load(open(imp_path))
tests = json.load(open(tests_path))["tests"]
num = {t["id"]: t["number"] for t in tests}

PFX = "/src/"
per_test = defaultdict(lambda: defaultdict(set))
opened = defaultdict(set)
class_setup = defaultdict(lambda: defaultdict(set))
class_opened = defaultdict(set)
unknown = set()


def cls_of(nodeid):
    return nodeid.rsplit("::", 1)[0]


for key, files in raw["lines"].items():
    if key in ("__import__", "__end__"):
        continue
    nodeid, _, phase = key.partition("|")
    if nodeid not in num:
        unknown.add(nodeid)
        continue
    for f, ls in files.items():
        f = f[len(PFX):] if f.startswith(PFX) else f
        if phase == "setup" or phase == "teardown":
            class_setup[cls_of(nodeid)][f].update(ls)
        per_test[num[nodeid]][f].update(ls)
for key, paths in raw["opened"].items():
    if key in ("__import__", "__end__"):
        continue
    nodeid, _, phase = key.partition("|")
    if nodeid not in num:
        continue
    ps = {p[len(PFX):] if p.startswith(PFX) else p for p in paths}
    if phase in ("setup", "teardown"):
        class_opened[cls_of(nodeid)].update(ps)
    opened_n = num[nodeid]
    for p in ps:
        opened[p].add(opened_n)

# class-level setup applies to every test of the class
for t in tests:
    c = cls_of(t["id"])
    for f, ls in class_setup.get(c, {}).items():
        per_test[t["number"]][f].update(ls)
    for p in class_opened.get(c, ()):
        opened[p].add(t["number"])

inv = defaultdict(lambda: defaultdict(set))
for n, files in per_test.items():
    for f, ls in files.items():
        for l in ls:
            inv[f][l].add(n)

set_index = {}
sets = []
lines_out = {}
for f, d in inv.items():
    m = {}
    for l, s in d.items():
        k = tuple(sorted(s))
        if k not in set_index:
            set_index[k] = len(sets)
            sets.append(list(k))
        m[str(l)] = set_index[k]
    lines_out[f] = m

imp_out = {}
for f, ls in imp.items():
    f = f[len(PFX):] if f.startswith(PFX) else f
    imp_out[f] = sorted(ls)

out = {
    "tests": [t["id"] for t in tests],
    "sets": sets,
    "lines": lines_out,
    "importlines": imp_out,
    "opened": {p: sorted(s) for p, s in opened.items()},
}
with gzip.open(out_path, "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print("unknown nodeids:", sorted(unknown)[:10], len(unknown), file=sys.stderr)
print("sets", len(sets), "files", len(lines_out), file=sys.stderr)
