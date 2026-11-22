"""Build time: per-iteration line coverage of fixture-driven tests.

Tests that loop over tests/helpers.load_sql_fixtures / load_sql_fixture_pairs run one
fixture case per iteration. For each such test this records which sqlglot lines every
iteration runs ("its") and which lines run outside any iteration ("main"). A later run
can skip the iterations that never reach a changed function body.

usage: python builditer.py <tests.json> <out.json.gz> <parallel>
A test is only marked filterable if it still passes at the pinned commit with half of
its iterations skipped (i.e. its iterations are independent).
"""
import gzip
import json
import os
import select
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
nodeids = [t["id"] for t in tests]
OUT, PAR = sys.argv[2], int(sys.argv[3])
runner.zygote_import({runner.module_name_for(t["file"]) for t in tests})
REPO = os.path.realpath(runner.REPO) + os.sep
PREFIX = REPO + "sqlglot" + os.sep
LOADERS = ("load_sql_fixtures", "load_sql_fixture_pairs")
users = sorted(i for i, t in enumerate(tests)
               if any(hasattr(sys.modules.get(runner.module_name_for(t["file"])), n) for n in LOADERS))
print("tests in modules using fixture loaders:", len(users), file=sys.stderr)


def trace(t):
    mon = sys.monitoring
    TOOL = 4
    mon.use_tool_id(TOOL, "briter")
    data = {None: {}}
    cur = [data[None]]
    counts = {}

    def switch(ctx):
        cur[0] = data.setdefault(ctx, {})
        mon.restart_events()

    def on_line(code, line):
        fn = code.co_filename
        if not fn.startswith(PREFIX):
            return mon.DISABLE
        cur[0].setdefault(fn[len(REPO):], set()).add(line)
        return mon.DISABLE

    # check_file submits in fixture order and checks results in completion
    # order. Attribute both halves to the same row, keeping unrelated test code
    # outside the loop in the original main context.
    runner.trace_fixture_comparisons(switch)
    runner.ITER_HOOK = (switch, counts)
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    for event in (mon.events.PY_START, mon.events.PY_RESUME):
        mon.register_callback(TOOL, event, lambda code, offset: on_line(code, code.co_firstlineno))
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START | mon.events.PY_RESUME)
    failed = runner.run_tests([t], nodeids, timeouts=[3000.0])
    mon.set_events(TOOL, 0)
    its = {}
    for ctx, files in data.items():
        if ctx is None:
            continue
        for f, ls in files.items():
            d = its.setdefault(f, {})
            for l in ls:
                d.setdefault(l, []).append(ctx)
    return {"failed": bool(failed), "n": sum(counts.values()), "calls": counts,
            "main": {f: sorted(v) for f, v in data[None].items()},
            "its": {f: {str(l): sorted(v) for l, v in d.items()} for f, d in its.items()}}


def half(t):
    """Baseline run with every other iteration skipped: must still pass."""
    runner.ITER_FILTER[t] = "even"
    failed = runner.run_tests([t], nodeids, timeouts=[3000.0])
    return {"failed": bool(failed)}


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
                results[it] = json.loads(b"".join(buf))
            except Exception:  # noqa
                results[it] = {"error": "bad output"}
    return results


runner.install_iter_wrappers()
res = fanout(users, trace)
cand = [t for t, r in res.items() if "error" not in r and not r["failed"] and r["n"] >= 4]
print("tests with iterations:", len(cand), file=sys.stderr)
ok = fanout(cand, half)
out = {}
for t in cand:
    if ok[t].get("failed") is False:
        r = res[t]
        out[str(t)] = {"n": r["n"], "main": r["main"], "its": r["its"]}
    else:
        print("not independent:", nodeids[t], file=sys.stderr)
with gzip.open(OUT, "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print("filterable tests", len(out), "size", os.path.getsize(OUT), file=sys.stderr)
for t in sorted(out, key=lambda t: -out[t]["n"])[:12]:
    print(" ", nodeids[int(t)], out[t]["n"], file=sys.stderr)
