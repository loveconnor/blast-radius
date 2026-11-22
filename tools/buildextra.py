"""Capture runtime test coverage missing from the pinned line map.

Run inside the selector image with PYTHONPATH=/app. Each test runs in a fresh
fork; function entries also count, including one-line lambdas on import lines.
"""
import gzip
import json
import os
import pickle
import sys
import tempfile
from pathlib import Path

import runner
from selcore import Map


def trace(t, tests, root):
    seen = {}
    monitor = sys.monitoring
    tool = 4
    prefix = root + "sqlglot/"

    def line(code, number):
        filename = code.co_filename
        if filename.startswith(prefix):
            seen.setdefault(filename[len(root):], set()).add(number)
        return monitor.DISABLE

    def enter(code, offset):
        return line(code, code.co_firstlineno)

    monitor.use_tool_id(tool, "test-coverage")
    monitor.register_callback(tool, monitor.events.LINE, line)
    monitor.register_callback(tool, monitor.events.PY_START, enter)
    monitor.register_callback(tool, monitor.events.PY_RESUME, enter)
    monitor.set_events(tool, monitor.events.LINE | monitor.events.PY_START | monitor.events.PY_RESUME)
    runner.run_tests([t], tests, timeouts=[150.0])
    monitor.set_events(tool, 0)
    return seen


def build(output):
    base = Map(Path(runner.__file__).with_name("map.json.gz"), supplement=False)
    tests = base.tests
    runner.zygote_import({runner.module_name_for(t.split("::")[0]) for t in tests})
    root = str(Path(runner.REPO).resolve()) + "/"
    additions = {}
    running = {}
    pending = iter(range(len(tests)))
    with tempfile.TemporaryDirectory() as tmp:
        while True:
            while len(running) < 3:
                t = next(pending, None)
                if t is None:
                    break
                file = Path(tmp) / str(t)
                pid = os.fork()
                if not pid:
                    try:
                        file.write_bytes(pickle.dumps(trace(t, tests, root)))
                        os._exit(0)
                    except BaseException:
                        os._exit(1)
                running[pid] = t, file
            if not running:
                break
            pid, status = os.wait()
            t, file = running.pop(pid)
            if status:
                continue
            reached = pickle.loads(file.read_bytes())
            file.unlink()
            for path, lines in reached.items():
                for number in lines:
                    if t not in base.covering(path, number, number):
                        additions.setdefault((path, number), []).append(t)
    sets = {}
    files = {}
    for (path, line), readers in sorted(additions.items()):
        key = tuple(sorted(readers))
        files.setdefault(path, {})[line] = sets.setdefault(key, len(sets))
    with gzip.open(output, "wt") as fh:
        json.dump({"tests": tests, "sets": list(sets), "lines": files}, fh, separators=(",", ":"))


if __name__ == "__main__":
    build(sys.argv[1])
