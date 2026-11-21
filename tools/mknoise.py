import sys, os, json, tempfile, importlib, pkgutil
sys.path.insert(0, "/w/tools"); os.environ["BR_REPO"] = "/w/sqlglot"
import runner, statediff
runner.zygote_import(set())
import sqlglot
def walk_all():
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not m.name.endswith("__main__"):
            try: importlib.import_module(m.name)
            except Exception: pass
walk_all()
allc = set()
for rep in range(3):
    rd, wr = os.pipe(); pid = os.fork()
    if pid == 0:
        old = {k: v for k, v in sys.modules.items() if k == "sqlglot" or k.startswith("sqlglot.")}
        tmp = tempfile.mkdtemp(); runner.build_overlay(tmp, {}); runner.reimport(tmp, set())
        import sqlglot as s2
        for m in pkgutil.walk_packages(s2.__path__, "sqlglot."):
            if not m.name.endswith("__main__"):
                try: importlib.import_module(m.name)
                except Exception: pass
        new = {k: v for k, v in sys.modules.items() if k == "sqlglot" or k.startswith("sqlglot.")}
        os.write(wr, json.dumps([list(c) for c in statediff.diff_state(old, new)]).encode()); os._exit(0)
    os.close(wr); buf = b""
    while (b := os.read(rd, 1 << 20)): buf += b
    os.waitpid(pid, 0); allc |= {tuple(c) for c in json.loads(buf)}
json.dump(sorted(allc, key=str), open("/w/out/noise.json", "w"))
print(sorted(allc, key=str))
