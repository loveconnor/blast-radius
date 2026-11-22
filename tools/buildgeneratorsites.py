"""Trace which pinned tests can distinguish keyed SQL comments changes."""
import ast
import gzip
import inspect
import importlib
import multiprocessing
import json
import os
import sys
import tempfile
from pathlib import Path

import generatorsites
import runner
from selcore import Map

coverage = Map(Path(runner.__file__).with_name("map.json.gz"))
tests = coverage.tests
root = Path(runner.REPO)
targets = {}
sites = {}
next_id = 0
for path in [root / generatorsites.GEN, *(root / "sqlglot/generators").glob("*.py")]:
    tree = ast.parse(path.read_bytes())
    rel = str(path.relative_to(root))
    for comment in generatorsites._keyed_calls(tree):
        if comment.lineno in coverage.importlines.get(rel, set()):
            continue
        site = next_id
        next_id += 1
        targets.setdefault(str(path), {}).setdefault(comment.lineno, []).append(site)
        sites.setdefault(rel, {}).setdefault(comment.lineno, []).append(
            [comment.col_offset, comment.end_col_offset, comment.value, site])

def cold(file):
    touched = set()
    monitor = sys.monitoring
    def start(code, offset):
        if code.co_filename in targets:
            monitor.set_local_events(4, code, monitor.events.LINE)
        return monitor.DISABLE
    def line(code, number):
        touched.update(targets[code.co_filename].get(number, ()))
        return monitor.DISABLE
    monitor.use_tool_id(4, "cold-generator-sites")
    monitor.register_callback(4, monitor.events.PY_START, start)
    monitor.register_callback(4, monitor.events.LINE, line)
    monitor.set_events(4, monitor.events.PY_START)
    try:
        importlib.import_module(runner.module_name_for(file))
    except Exception:
        return file, set(range(next_id))
    return file, touched


def record(t):
    seen, unsafe = set(), set()
    escaped = False
    monitor = sys.monitoring
    tool = 4
    def audit(event, args):
        nonlocal escaped
        if event in ("subprocess.Popen", "os.posix_spawn", "os.fork"):
            escaped = True
    sys.addaudithook(audit)
    def start(code, offset):
        if code.co_filename in targets:
            monitor.set_local_events(tool, code, monitor.events.LINE)
        return monitor.DISABLE
    def line(code, number):
        ids = targets[code.co_filename].get(number)
        if not ids:
            return monitor.DISABLE
        seen.update(ids)
        receiver = sys._getframe(1).f_locals.get("self")
        if receiver is None or inspect.getattr_static(receiver, "sql", None) is not sql:
            unsafe.update(ids)
    monitor.use_tool_id(tool, "generator-sites")
    monitor.register_callback(tool, monitor.events.PY_START, start)
    monitor.register_callback(tool, monitor.events.LINE, line)
    monitor.set_events(tool, monitor.events.PY_START)
    failed = runner.run_tests([t], tests, timeouts=[180])
    if escaped or failed:
        return []
    return sorted(seen - unsafe)

def build(output):
    global sql
    sys.path.insert(0, runner.REPO)
    files = set(coverage.file_of)
    with multiprocessing.get_context("fork").Pool(2, maxtasksperchild=1) as pool:
        imported = dict(pool.imap_unordered(cold, sorted(files)))
    runner.zygote_import({runner.module_name_for(f) for f in files})
    from sqlglot.generator import Generator
    sql = Generator.sql
    allowed = [set() for _ in range(next_id)]
    pending = iter(range(len(tests)))
    running = {}
    with tempfile.TemporaryDirectory() as tmp:
        while True:
            while len(running) < 2:
                t = next(pending, None)
                if t is None:
                    break
                file = Path(tmp) / str(t)
                pid = os.fork()
                if not pid:
                    try:
                        file.write_text(json.dumps(record(t)))
                        os._exit(0)
                    except BaseException:
                        os._exit(1)
                running[pid] = t, file
            if not running:
                break
            pid, status = os.wait()
            t, file = running.pop(pid)
            if not status:
                observed = set(json.loads(file.read_text())) - imported[coverage.file_of[t]]
                for site in observed:
                    allowed[site].add(t)
    if len(sys.argv) > 2:
        with gzip.open(sys.argv[2], "rt") as fh:
            prev = json.load(fh)
        assert prev["tests"] == tests
        for rel, table in sites.items():
            for n, records in table.items():
                for col, _, _, site in records:
                    earlier = next(row for row in prev["sites"][rel][str(n)] if row[0] == col)
                    allowed[site].intersection_update(prev["groups"][earlier[3]])
    groups = {}
    def group(site):
        key = tuple(sorted(allowed[site]))
        return groups.setdefault(key, len(groups))
    for table in sites.values():
        for records in table.values():
            for row in records:
                row[3] = group(row[3])
    with gzip.open(output, "wt") as fh:
        json.dump({"tests": tests, "sites": sites, "groups": list(groups)}, fh)
    print("observed sites", next_id)

if __name__ == "__main__":
    build(sys.argv[1])
