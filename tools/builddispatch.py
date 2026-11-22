"""Build baseline test coverage for concrete generator dispatch lookups.

Run inside the selector image with /app on PYTHONPATH. Arguments are the
pinned tests.json and the destination .json.gz file.
"""
import collections
import gzip
import json
import multiprocessing
import sys

import runner


def typename(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def collect(test):
    seen.clear()
    other.clear()
    bad = runner.run_tests([test], ids, timeouts=[300.0])
    return test, list(seen), bool(bad or other)


def cold_import(file):
    """Observe generator lookups before the test module and its dependencies finish importing."""
    import importlib
    trace = set()
    def line(code, number):
        if code.co_filename.endswith("/sqlglot/generator.py") and number == 1202 and code.co_name == "sql":
            frame = sys._getframe(1)
            trace.add((typename(type(frame.f_locals["self"])), typename(type(frame.f_locals["expression"]))))
            return None
        return sys.monitoring.DISABLE

    mon = sys.monitoring
    mon.use_tool_id(4, "dispatch-imports")
    mon.register_callback(4, mon.events.LINE, line)
    mon.set_events(4, mon.events.LINE)
    try:
        importlib.import_module(runner.module_name_for(file))
        return file, list(trace), False
    except Exception:
        return file, [], True
    finally:
        mon.set_events(4, 0)


if __name__ == "__main__":
    tests = json.load(open(sys.argv[1]))["tests"]
    ids = [row["id"] for row in tests]
    sys.path.insert(0, runner.REPO)
    files = sorted({row["file"] for row in tests})
    with multiprocessing.get_context("fork").Pool(5, maxtasksperchild=1) as pool:
        imports = {f: (pairs, bad) for f, pairs, bad in pool.imap_unordered(cold_import, files)}
    runner.zygote_import({runner.module_name_for(row["file"]) for row in tests})
    from sqlglot import generator
    from sqlglot.dialects.dialect import Dialect

    classes = {cls.generator_class for cls in Dialect.classes.values()} | {generator.Generator}
    seen, other = set(), set()
    original = generator._build_dispatch

    class Dispatch(dict):
        def get(self, expression, default=None):
            seen.add((self.owner, typename(expression)))
            return super().get(expression, default)

    def build(cls):
        table = Dispatch(original(cls))
        table.owner = typename(cls)
        if cls not in classes:
            other.add(table.owner)
        return table

    generator._build_dispatch = build
    generator._DISPATCH_CACHE.clear()
    index = collections.defaultdict(lambda: collections.defaultdict(list))
    known = set(map(typename, classes))
    unknown = []
    # Each test inherits the pristine parent. This includes setUpClass and
    # avoids recording a later test against the previous test's warmed cache.
    with multiprocessing.get_context("fork").Pool(5, maxtasksperchild=1) as pool:
        for t, pairs, bad in pool.imap_unordered(collect, range(len(ids)), chunksize=1):
            init, init_bad = imports[tests[t]["file"]]
            for owner, expr in set(init):
                index[owner][expr].append(t)
            if bad or init_bad or any(owner not in known for owner, _ in init):
                unknown.append(t)
            for owner, expr in pairs:
                index[owner][expr].append(t)
    with gzip.open(sys.argv[2], "wt") as fh:
        json.dump({"ids": ids, "classes": sorted(map(typename, classes)), "reads": index,
                   "unknown": sorted(unknown)}, fh, separators=(",", ":"))
