"""Build-time: which sqlglot modules does each test import (directly or lazily)?

Every test module gets a fresh interpreter that has imported only sqlglot and that test
module; each of its tests then runs in a fork, and the sqlglot modules that appeared in
sys.modules are the ones that test needs beyond the module's own imports.

usage: python buildimports.py <tests.json> <out.json.gz> <parallel>        (driver)
       python buildimports.py --module <tests.json> <module file>           (one module)
"""
import gzip
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def sq_mods():
    return {m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")}


def one_module(tests_path, rel):
    os.environ["BR_WARM"] = "0"
    os.environ.setdefault("BR_REPO", "/w/sqlglot")
    sys.path.insert(0, HERE)
    import runner
    tests = json.load(open(tests_path))["tests"]
    nodeids = [t["id"] for t in tests]
    mine = [i for i, t in enumerate(nodeids) if t.split("::")[0] == rel]
    runner.zygote_import(set())
    import sqlglot  # noqa
    base0 = sq_mods()
    err = None
    try:
        __import__(runner.module_name_for(rel))
    except BaseException as e:  # noqa
        err = repr(e)
    base = sq_mods()
    out = {"base": sorted(base), "err": err, "tests": {}}
    for t in mine:
        rd, wr = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rd)
            try:
                failed = runner.run_tests([t], nodeids, timeouts=[300.0])
                res = {"new": sorted(sq_mods() - base), "failed": bool(failed)}
            except BaseException as e:  # noqa
                res = {"new": None, "failed": True, "err": repr(e)}
            os.write(wr, json.dumps(res).encode())
            os._exit(0)
        os.close(wr)
        buf = b""
        while True:
            ch = os.read(rd, 1 << 16)
            if not ch:
                break
            buf += ch
        os.close(rd)
        os.waitpid(pid, 0)
        out["tests"][t] = json.loads(buf or b'{"new": null, "failed": true}')
    sys.__stdout__.write("\nBRIMPORTS " + json.dumps(out) + "\n")
    sys.__stdout__.flush()


def main():
    tests_path, out_path, par = sys.argv[1], sys.argv[2], int(sys.argv[3])
    tests = json.load(open(tests_path))["tests"]
    files = sorted({t["id"].split("::")[0] for t in tests})
    procs, results = [], {}
    todo = list(files)

    def reap(p, rel):
        so, _ = p.communicate()
        for line in so.decode("utf-8", "replace").splitlines():
            if line.startswith("BRIMPORTS "):
                results[rel] = json.loads(line[10:])
        if rel not in results:
            print("no result for", rel, file=sys.stderr)
    while todo or procs:
        while todo and len(procs) < par:
            rel = todo.pop(0)
            procs.append((subprocess.Popen([sys.executable, __file__, "--module", tests_path, rel],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL), rel))
        p, rel = procs.pop(0)
        reap(p, rel)
    names, idx = [], {}

    def ids(ms):
        out = []
        for m in ms:
            if m not in idx:
                idx[m] = len(names)
                names.append(m)
            out.append(idx[m])
        return sorted(out)
    sets, sidx, per_test, unknown = [], {}, {}, []
    nodeids = [t["id"] for t in tests]
    for i, nid in enumerate(nodeids):
        r = results.get(nid.split("::")[0])
        tr = r and (r["tests"].get(str(i)) or r["tests"].get(i))
        if not r or not tr or tr.get("new") is None or tr.get("failed"):
            unknown.append(i)
            continue
        key = tuple(ids(set(r["base"]) | set(tr["new"])))
        if key not in sidx:
            sidx[key] = len(sets)
            sets.append(list(key))
        per_test[i] = sidx[key]
    with gzip.open(out_path, "wt") as fh:
        json.dump({"mods": names, "sets": sets, "tests": per_test, "unknown": unknown}, fh)
    print(f"{len(per_test)} tests, {len(unknown)} unknown, {len(sets)} distinct sets, {len(names)} modules")


if __name__ == "__main__":
    if sys.argv[1] == "--module":
        one_module(sys.argv[2], sys.argv[3])
    else:
        main()
