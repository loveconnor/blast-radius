"""Per-submit coverage for pool-based executor tests -> submap.json.gz"""
import json, gzip, sys
raw = json.load(open(sys.argv[1]))["lines"]
tests = json.load(open(sys.argv[2]))["tests"]; num = {t["id"]: t["number"] for t in tests}
TARGETS = ["tests/test_executor.py::TestExecutor::test_execute_tpcds", "tests/test_executor.py::TestExecutor::test_execute_tpch"]
P = "/src/"
out = {}
# class-level setup lines (all |setup and |teardown keys of the class)
cls_setup = {}
for k, files in raw.items():
    if k.startswith("tests/test_executor.py::TestExecutor::") and ("|setup" in k or "|teardown" in k):
        for f, ls in files.items():
            cls_setup.setdefault(f, set()).update(ls)
for t in TARGETS:
    main = {}
    for f, ls in list(raw.get(t, {}).items()) + list(cls_setup.items()):
        main.setdefault(f[len(P):], set()).update(ls)
    subs = {}
    n = 0
    for k, files in raw.items():
        if k.startswith(t + "#"):
            i = int(k.split("#")[1]); n = max(n, i + 1)
            for f, ls in files.items():
                d = subs.setdefault(f[len(P):], {})
                for l in ls:
                    d.setdefault(l, []).append(i)
    out[num[t]] = {"n": n, "main": {f: sorted(v) for f, v in main.items()},
                   "subs": {f: {str(l): sorted(v) for l, v in d.items()} for f, d in subs.items()}}
    print(t, n, sum(len(v) for v in main.values()), file=sys.stderr)
with gzip.open(sys.argv[3], "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
