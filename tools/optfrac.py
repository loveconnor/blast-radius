"""Per test: share of its time spent inside optimize() calls (default rules, copy=True)."""
import sys, os, json, time, select
sys.path.insert(0, "/w/tools"); os.environ.setdefault("BR_REPO", "/w/sqlglot")
import runner, optmemo
tests = json.load(open("/kit/sample/tests.json"))["tests"]; nodeids = [t["id"] for t in tests]
runner.zygote_import({runner.module_name_for(t["file"]) for t in tests})
import sqlglot.optimizer.optimizer as O
orig = O.optimize; acc = [0.0]; depth = [0]
def timed(expression, schema=None, db=None, catalog=None, dialect=None, rules=None, sql=None, copy=True, **kw):
    if rules is None: rules = O.RULES
    if depth[0] or rules is not O.RULES or not copy:
        return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kw)
    depth[0] += 1; t = time.perf_counter()
    try: return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kw)
    finally: depth[0] -= 1; acc[0] += time.perf_counter() - t
optmemo.optimize = timed; timed.__module__ = "optmemo"; timed.__qualname__ = "optimize"
optmemo.replace_everywhere(orig, timed)
out = {}
ids = [i for i in range(len(tests)) if i != 1490]
for i in ids:
    rd, wr = os.pipe(); pid = os.fork()
    if pid == 0:
        t = time.perf_counter(); runner.run_tests([i], nodeids, timeouts=[900.0]); tot = time.perf_counter() - t
        os.write(wr, json.dumps([acc[0], tot]).encode()); os._exit(0)
    os.close(wr); data = b""
    while True:
        ch = os.read(rd, 65536)
        if not ch: break
        data += ch
    os.close(rd); os.waitpid(pid, 0)
    a, tot = json.loads(data)
    if a > 0.02 and a / tot > 0.05: out[i] = round(a / tot, 3)
json.dump(out, open("/w/out/optfrac.json", "w"))
print(len(out), sorted(out.items(), key=lambda kv: -kv[1])[:12])
