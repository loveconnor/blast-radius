"""Record the code and class settings reached by each full-sized scope stress case."""
import hashlib
import inspect
import pickle
import sys
import unittest
from pathlib import Path

import lineevents
import runner
import scopeblocks
import settingtrace

runner.zygote_import({"tests.test_optimizer"})
from tests.test_optimizer import TestOptimizer

method = TestOptimizer.test_scope
source, _, groups = scopeblocks.source_blocks(method)
offset = inspect.getsourcelines(method)[1] - 1
locations = {line + offset: i for i, group in enumerate(groups)
             for line in range(group[0].lineno, group[-1].end_lineno + 1)}
blocks = [({}, set()) for _ in groups]
current = None


def setting(key):
    if current is not None:
        blocks[current][1].add(key)


tracked = settingtrace.install(setting)
root = str(Path(runner.REPO)) + "/"
prefix = root + "sqlglot/"
monitor = sys.monitoring
tool = 4


def line(code, number):
    global current
    if code is method.__code__:
        block = locations.get(number)
        if block != current:
            current = block
            monitor.restart_events()
        return None
    if current is not None and code.co_filename.startswith(prefix):
        blocks[current][0].setdefault(code.co_filename[len(root):], set()).add(number)
    return monitor.DISABLE


monitor.use_tool_id(tool, "union-chain")
events = lineevents.register(monitor, tool, line)
monitor.set_events(tool, events)
result = unittest.TestSuite([TestOptimizer("test_scope")]).run(unittest.TestResult())
monitor.set_events(tool, 0)
if not result.wasSuccessful() or any(not paths for paths, _ in blocks):
    raise RuntimeError(f"scope trace incomplete: {result.errors} {result.failures}")
with scopeblocks.INDEX.open("wb") as fh:
    pickle.dump({"source": hashlib.sha256(source.encode()).digest(), "tracked": tracked, "blocks": blocks}, fh)
