import json, os, sys, time, unittest, importlib
sys.path.insert(0, "/w/tools"); os.environ["BR_REPO"] = "/opt/sqlglot"
import runner
tests = json.load(open("/data/tests.json"))["tests"]
runner.zygote_import({runner.module_name_for(t["file"]) for t in tests})
by = {}
for t in tests:
    p = t["id"].split("::"); by.setdefault((runner.module_name_for(p[0]), p[1]), []).append((t["number"], p[2]))
out = {"test": {}, "class": {}}
for (mod, cls), items in by.items():
    C = getattr(importlib.import_module(mod), cls)
    suite = unittest.TestSuite(); per = {}
    for n, m in items:
        c = C(m); r = c.run
        def w(*a, _r=r, _n=n, **k):
            s = time.process_time(); 
            try: return _r(*a, **k)
            finally: per[_n] = time.process_time() - s
        c.run = w; suite.addTest(c)
    s = time.process_time(); suite.run(unittest.TestResult()); tot = time.process_time() - s
    out["test"].update({str(k): round(v, 4) for k, v in per.items()})
    out["class"][f"{mod}::{cls}"] = round(max(0.0, tot - sum(per.values())), 4)
    print(mod, cls, round(tot, 2), file=sys.stderr, flush=True)
json.dump(out, sys.stdout)
