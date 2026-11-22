"""Capture isolated optimizer fixture worker calls on the unmodified checkout."""
import concurrent.futures
import gzip
import hashlib
import json
import pickle
import sys
import time
import zlib
from pathlib import Path

import poolmemo
import runner
import settingtrace

runner.zygote_import({"tests.test_optimizer"})
import tests.test_optimizer as optimizer_tests

root = str(Path(runner.REPO)) + "/"
monitor = sys.monitoring
tool = 4
current = None
records = {}
observed = {}
settings = {}
current_settings = None
invalid = set()
costs = {}


def setting(key):
    if current_settings is not None:
        current_settings.add(key)


tracked = settingtrace.install(setting, schemas=True)


def line(code, number):
    if current is not None and code.co_filename.startswith(root + "sqlglot/"):
        current.add((code.co_filename[len(root):], number))
    return monitor.DISABLE


original = runner.InlineExecutor.submit


def submit(pool, fn, *args, **kwargs):
    global current, current_settings
    if fn is not optimizer_tests.parse_and_optimize:
        return original(pool, fn, *args, **kwargs)
    future = concurrent.futures.Future()
    payload = pickle.dumps((fn, args, kwargs))
    key = hashlib.sha256(payload).digest()
    started = time.process_time()
    current = observed.setdefault(key, set())
    current_settings = settings.setdefault(key, set())
    monitor.restart_events()
    try:
        work, a, k = pickle.loads(payload)
        value = work(*a, **k)
        raw = pickle.dumps(value)
        result = pickle.loads(raw)
        future.set_result(result)
    except Exception as error:
        invalid.add(key)
        future.set_exception(error)
    finally:
        current = current_settings = None
        seconds = time.process_time() - started
        timing[key] = timing.get(key, 0.0) + seconds
    if key not in invalid:
        # Reject a worker whose serialized output varies on identical input.
        blob = zlib.compress(raw, 3)
        if records.setdefault(key, blob) != blob:
            invalid.add(key)
    return future


monitor.use_tool_id(tool, "fixture-worker")
import lineevents
events = lineevents.register(monitor, tool, line)
runner.InlineExecutor.submit = submit
with gzip.open(Path(__file__).with_name("iter.json.gz"), "rt") as fh:
    fixture_tests = json.load(fh)
with gzip.open(Path(__file__).with_name("map.json.gz"), "rt") as fh:
    tests = json.load(fh)["tests"]
monitor.set_events(tool, events)
for t in map(int, fixture_tests):
    if not tests[t].startswith("tests/test_optimizer.py::"):
        continue
    started = time.process_time()
    timing = {}
    failed = runner.run_tests([t], tests, timeouts=[300.0])
    if not failed and timing:
        costs[t] = time.process_time() - started, timing
    elif failed:
        invalid.update(timing)
monitor.set_events(tool, 0)
entries = {}
lines = {}
reads = {}
with poolmemo.BINARY.open("wb") as fh:
    for key in sorted(records.keys() - invalid):
        i = len(entries)
        blob = records[key]
        entries[key] = i, fh.tell(), len(blob)
        fh.write(blob)
        for attr in settings[key]:
            reads[attr] = reads.get(attr, 0) | (1 << i)
        for path, n in observed[key]:
            table = lines.setdefault(path, {})
            table[n] = table.get(n, 0) | (1 << i)
profiles = {t: (elapsed, {entries[k][0]: secs for k, secs in times.items() if k in entries})
            for t, (elapsed, times) in costs.items()}
with poolmemo.INDEX.open("wb") as fh:
    pickle.dump({"keys": entries, "lines": lines, "costs": profiles, "tracked": tracked, "reads": reads}, fh)
print("fixture workers", len(entries), "tests", len(profiles))
