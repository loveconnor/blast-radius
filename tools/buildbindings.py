"""Index class-field reads that share a name with a SQLGlot module dictionary.

Run in the entry image with this tools directory mounted. Pass an output path
and, optionally, the previous hash-seed run to intersect the two observations.
"""
import ast
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/app")
import runner
from selcore import Map

coverage = Map("/app/map.json.gz")
root = Path(runner.REPO)
runner.zygote_import(set())
from sqlglot.dialects.dialect import Dialect

classes = set(Dialect.classes.values()) | {Dialect}
dicts = {k for c in classes for k, v in vars(c).items() if k.isupper() and type(v) is dict}
names = {k for mod, obj in tuple(sys.modules.items()) if mod.startswith("sqlglot.")
         for k, v in vars(obj).items() if k in dicts and type(v) is dict}
sites = {}
participants = set()
index = []
for path in sorted((root / "sqlglot").rglob("*.py")):
    src = path.read_text()
    rel = str(path.relative_to(root))
    lines = src.splitlines()
    for expr in ast.walk(ast.parse(src)):
        if (not isinstance(expr, ast.Attribute) or not isinstance(expr.ctx, ast.Load)
                or expr.attr not in names or not isinstance(expr.value, ast.Name)
                or expr.lineno != expr.end_lineno):
            continue
        if re.findall(r"\b" + expr.attr + r"\b", lines[expr.lineno - 1]) != [expr.attr]:
            continue
        covered = coverage.covering(rel, expr.lineno, expr.lineno)
        if not covered:
            continue
        participants.update(covered)
        sites.setdefault(rel, {})[f"{expr.lineno}|{expr.attr}"] = len(index)
        index.append((rel, expr.lineno, expr.attr))

plugin = """
import mmap, os, json
from pathlib import Path
import pytest
root = Path(os.environ["BR_BIND_RUN"])
ids = json.loads((root / "ids.json").read_text())
with (root / "current").open("r+b") as f:
    current = mmap.mmap(f.fileno(), 4)
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    current[:] = ids[item.nodeid].to_bytes(4, "little", signed=True)
    try:
        yield
    finally:
        current[:] = (-1).to_bytes(4, "little", signed=True)
"""
seen = {}
tools = str(Path(__file__).resolve().parent)


def batch(tests):
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "current").write_bytes((-1).to_bytes(4, "little", signed=True))
        (directory / "sites.json").write_text(json.dumps(sites))
        ids = {coverage.tests[t]: t for t in tests}
        (directory / "ids.json").write_text(json.dumps(ids))
        (directory / "bindingplugin.py").write_text(plugin)
        (directory / "sitecustomize.py").write_text(
            "import os, bindinghook\nbindinghook.install(os.environ['BR_BIND_RUN'], os.environ['BR_REPO'])\n")
        env = dict(os.environ, BR_BIND_RUN=tmp, BR_REPO=str(root), PYTHONPATH=os.pathsep.join((tmp, tools, str(root))))
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "bindingplugin", *ids],
                                env=env, cwd=root, stdout=subprocess.DEVNULL)
        workers = {f.suffix for f in directory.glob("worker.*")}
        boots = {f.suffix for f in directory.glob("boot.*")}
        if result.returncode or not boots or not workers <= boots or list(directory.glob("escaped.*")):
            return False
        def events(prefix):
            return {tuple(map(int, f.stem[len(prefix):].split("-", 1))) for f in directory.glob(prefix + "*")}
        invalid = events("unsafe-")
        for site, test in events("seen-") - invalid:
            if test >= 0 and (site, -1) not in invalid:
                seen.setdefault(site, set()).add(test)
        return True


ordered = sorted(participants)
for pos in range(0, len(ordered), 30):
    group = ordered[pos:pos + 30]
    if not batch(group):
        for t in group:
            batch([t])
result = {"|".join(map(str, index[i])): sorted(ns) for i, ns in seen.items()}
if len(sys.argv) > 2:
    with gzip.open(sys.argv[2], "rt") as f:
        old = json.load(f)
    assert old["tests"] == coverage.tests
    result = {k: sorted(set(v).intersection(old["sites"].get(k, ()))) for k, v in result.items()}
with gzip.open(sys.argv[1], "wt") as f:
    json.dump({"tests": coverage.tests, "sites": result}, f)
print({k: len(v) for k, v in result.items()})
