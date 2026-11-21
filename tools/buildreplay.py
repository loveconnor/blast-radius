"""Build time: record every test's parse-phase calls and its out-of-phase line coverage.

usage: python buildreplay.py <tests.json> <out.json.gz> <parallel>
Each test runs in its own fork (so class setup is part of every test of the class).
Output:
  keys/dig/ms : unique phase inputs, their baseline digest and cost
  tests       : test number -> key ids
  bad         : tests that are not replayable (unrecordable or unstable calls, failures)
  out/sets    : sqlglot file -> line -> tests that run the line outside a phase
"""
import gzip
import json
import os
import select
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner  # noqa: E402

if sys.argv[1] == "--verify":
    # fresh interpreter (own hash seed): which recorded inputs do not reproduce their digest
    data = json.load(gzip.open(sys.argv[2], "rt"))
    tests = json.load(open(sys.argv[3]))["tests"]
    runner.zygote_import({runner.module_name_for(t["file"]) for t in tests})
    import replay as _r
    wrong = []
    for i, k in enumerate(data["keys"]):
        try:
            if _r.run_key(k) != data["dig"][i]:
                wrong.append(i)
        except BaseException:  # noqa
            wrong.append(i)
    print("WRONG " + json.dumps(wrong))
    sys.exit(0)

tests = json.load(open(sys.argv[1]))["tests"]
nodeids = [t["id"] for t in tests]
OUT, PAR = sys.argv[2], int(sys.argv[3])
runner.zygote_import({runner.module_name_for(t["file"]) for t in tests})
import replay  # noqa: E402

replay.install_recorder()
REPO = os.path.realpath(runner.REPO) + os.sep
PREFIX = REPO + "sqlglot" + os.sep


def run_child(t):
    mon = sys.monitoring
    TOOL = 4
    mon.use_tool_id(TOOL, "brreplay")
    lines = {}
    PH = replay.PH

    def on_line(code, line):
        fn = code.co_filename
        if not fn.startswith(PREFIX):
            return mon.DISABLE
        if PH["depth"]:
            return None
        lines.setdefault(fn[len(REPO):], set()).add(line)
        return mon.DISABLE

    # functions entered inside each phase call
    ftab, fidx, cur = [], {}, set()

    def on_start(code, offset):
        if not PH["depth"]:
            return None
        i = fidx.get(code)
        if i is None:
            fn = code.co_filename
            if not fn.startswith(PREFIX):
                fidx[code] = -1
                return None
            ls = [l for _, _, l in code.co_lines() if l]
            i = fidx[code] = len(ftab)
            ftab.append([fn[len(REPO):], min(ls) if ls else code.co_firstlineno, max(ls) if ls else code.co_firstlineno])
        if i >= 0:
            cur.add(i)
        return None

    class Rec(list):
        def append(self, item):
            list.append(self, tuple(item) + (sorted(cur),))
            cur.clear()

        def extend(self, items):
            snap = sorted(cur)
            cur.clear()
            for it in items:
                list.append(self, tuple(it) + (snap,))

    replay.REC = Rec()
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.register_callback(TOOL, mon.events.PY_RESUME, on_start)   # generators resumed in a later phase
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START | mon.events.PY_RESUME)
    failed = runner.run_tests([t], nodeids, timeouts=[3000.0])
    mon.set_events(TOOL, 0)
    return {"t": t, "failed": bool(failed), "unrec": replay.UNREC[0], "rec": list(replay.REC), "ftab": ftab,
            "lines": {f: sorted(v) for f, v in lines.items()}}


def fanout(items, work):
    results, running, todo = {}, {}, list(items)
    while todo or running:
        while todo and len(running) < PAR:
            it = todo.pop(0)
            rd, wr = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(rd)
                try:
                    data = json.dumps(work(it)).encode()
                except BaseException as e:  # noqa
                    data = json.dumps({"error": repr(e)}).encode()
                while data:
                    data = data[os.write(wr, data):]
                os._exit(0)
            os.close(wr)
            running[rd] = (pid, it, [])
        r, _, _ = select.select(list(running), [], [], 5)
        for fd in r:
            pid, it, buf = running[fd]
            ch = os.read(fd, 1 << 20)
            if ch:
                buf.append(ch)
                continue
            os.close(fd)
            os.waitpid(pid, 0)
            del running[fd]
            try:
                results[it if not isinstance(it, list) else it[0]] = json.loads(b"".join(buf))
            except Exception:  # noqa
                results[it if not isinstance(it, list) else it[0]] = {"error": "bad output"}
            if len(results) % 200 == 0:
                print(len(results), file=sys.stderr, flush=True)
    return results


res = fanout(range(len(tests)), run_child)
keys, dig, ms, bad_keys = {}, [], [], set()
gfid, kfuncs = {}, {}   # (file, first line, last line) -> function id; key id -> function ids
per_test, bad, out_lines = {}, set(), {}
for t, r in res.items():
    if "error" in r or r["failed"] or r["unrec"]:
        bad.add(t)
    if "error" in r:
        continue
    ids = []
    lf = [gfid.setdefault(tuple(x), len(gfid)) for x in r["ftab"]]
    for k, d, dt, fs in r["rec"]:
        i = keys.get(k)
        if i is None:
            i = keys[k] = len(dig)
            dig.append(d)
            ms.append(dt * 1000)
        elif dig[i] != d:
            bad_keys.add(i)
        if d is None:
            bad_keys.add(i)
        ms[i] = min(ms[i], dt * 1000)
        ids.append(i)
        kf = kfuncs.setdefault(i, set())
        for f in fs:
            kf.add(lf[f])
    per_test[t] = sorted(set(ids))
    for f, ls in r["lines"].items():
        d = out_lines.setdefault(f, {})
        for l in ls:
            d.setdefault(l, []).append(t)
print("tests", len(res), "bad", len(bad), "keys", len(dig), "unstable keys", len(bad_keys), file=sys.stderr)

# self-consistency: every key replayed in isolation must reproduce its digest
key_list = [json.loads(k) for k in keys]
chunks = [list(range(i, len(key_list), PAR * 4)) for i in range(PAR * 4)]


def verify(chunk):
    import time
    wrong, cost = [], {}
    for i in chunk:
        t = time.perf_counter()
        try:
            if replay.run_key(key_list[i]) != dig[i]:
                wrong.append(i)
        except BaseException:  # noqa
            wrong.append(i)
        cost[i] = (time.perf_counter() - t) * 1000
    return {"wrong": wrong, "cost": cost}


for r in fanout(chunks, verify).values():
    bad_keys.update(r.get("wrong", []))
    for i, c in r.get("cost", {}).items():
        ms[int(i)] = c   # cost of the replay itself, without line tracing
    if "error" in r:
        raise SystemExit("verify failed: " + r["error"])
print("keys not reproducible in isolation:", len(bad_keys), file=sys.stderr)
for t, ids in per_test.items():
    if bad_keys.intersection(ids):
        bad.add(t)
print("non-replayable tests", len(bad), file=sys.stderr)

sets, set_ix, out = [], {}, {}
for f, d in out_lines.items():
    o = out[f] = {}
    for l, ts in d.items():
        key = tuple(sorted(ts))
        i = set_ix.get(key)
        if i is None:
            i = set_ix[key] = len(sets)
            sets.append(list(key))
        o[str(l)] = i
# function id -> bitmap over key ids (which recorded inputs enter the function)
nk = len(dig)
rows = [0] * len(gfid)
for k, fs in kfuncs.items():
    for f in fs:
        rows[f] |= 1 << k
nb = (nk + 7) // 8
with gzip.open(OUT.replace(".json.gz", "") + ".fk.gz", "wb") as fh:
    for r in rows:
        fh.write(r.to_bytes(nb, "little"))
funcs = [None] * len(gfid)
for key, i in gfid.items():
    funcs[i] = list(key)
print("functions", len(funcs), "bitmap bytes", nb * len(rows), file=sys.stderr)
def write():
    with gzip.open(OUT, "wt") as fh:
        json.dump({"keys": key_list, "dig": dig, "ms": [round(x, 3) for x in ms],
                   "tests": {str(t): v for t, v in per_test.items()}, "bad": sorted(bad), "funcs": funcs,
                   "out": out, "sets": sets}, fh, separators=(",", ":"))


write()
# outputs that depend on the interpreter's hash seed are not usable: find them with other seeds
import subprocess
flaky = set()
for seed in ("3", "777", "4242", "99991"):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--verify", OUT, sys.argv[1]],
                       env=dict(os.environ, PYTHONHASHSEED=seed), capture_output=True, text=True)
    line = [l for l in r.stdout.splitlines() if l.startswith("WRONG ")]
    if not line:
        raise SystemExit("verify run failed: " + r.stderr[-500:])
    flaky.update(json.loads(line[0][6:]))
flaky -= bad_keys
for t, ids in per_test.items():
    if flaky.intersection(ids):
        bad.add(t)
print("seed-dependent keys", len(flaky), "non-replayable tests", len(bad), file=sys.stderr)
write()
print("written", OUT, os.path.getsize(OUT), file=sys.stderr)
sys.exit(0)
with gzip.open(OUT, "wt") as fh:
    json.dump({"keys": key_list, "dig": dig, "ms": [round(x, 3) for x in ms],
               "tests": {str(t): v for t, v in per_test.items()}, "bad": sorted(bad), "funcs": funcs,
               "out": out, "sets": sets}, fh, separators=(",", ":"))
print("written", OUT, os.path.getsize(OUT), file=sys.stderr)
