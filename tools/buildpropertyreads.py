"""Record expression class names read by the pinned SQLGlot deserializer.

Run in the selector image with /app on PYTHONPATH, passing tests.json and
the output .json.gz. Each test starts in a fresh fork of the warmed suite.
"""
import collections
import gzip
import importlib
import json
import multiprocessing
import sys

import runner


def watch(collect):
    mon = sys.monitoring
    mon.use_tool_id(4, "expression-class-reads")

    def line(code, number):
        if code.co_name == "_load" and code.co_filename.endswith("/sqlglot/serde.py"):
            # Observe the raw payload before _load splits a qualified class name.
            if number == code.co_firstlineno + 1:
                payload = sys._getframe(1).f_locals["payload"]
                name = payload.get("c")
                if isinstance(name, str):
                    collect.add(name.rsplit(".", 1)[-1])
            else:
                return mon.DISABLE
        else:
            return mon.DISABLE

    mon.register_callback(4, mon.events.LINE, line)
    mon.set_events(4, mon.events.LINE)


def cold(file):
    collect = set()
    watch(collect)
    try:
        importlib.import_module(runner.module_name_for(file))
        return file, collect, False
    except Exception:
        return file, collect, True


def run(number):
    collect = set()
    watch(collect)
    failed = runner.run_tests([number], ids, timeouts=[300.0])
    return number, collect, bool(failed)


if __name__ == "__main__":
    tests = json.load(open(sys.argv[1]))["tests"]
    ids = [row["id"] for row in tests]
    sys.path.insert(0, runner.REPO)
    files = sorted({row["file"] for row in tests})
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(4, maxtasksperchild=1) as pool:
        imports = {f: (names, bad) for f, names, bad in pool.imap_unordered(cold, files)}
    runner.zygote_import({runner.module_name_for(file) for file in files})
    import inspect
    from sqlglot import serde
    first = inspect.getsourcelines(serde._load)[0][1].strip()
    assert serde.CLASS == "c" and first == "class_name: str = payload[CLASS]", first
    reads = collections.defaultdict(list)
    unknown = []
    with ctx.Pool(4, maxtasksperchild=1) as pool:
        for number, names, failed in pool.imap_unordered(run, range(len(ids))):
            startup, bad = imports[tests[number]["file"]]
            for name in names | startup:
                reads[name].append(number)
            if failed or bad:
                unknown.append(number)
    assert reads["Select"], "deserializer was not observed"
    with gzip.GzipFile(sys.argv[2], "wb", mtime=0) as out:
        out.write(json.dumps({"tests": ids, "reads": {k: sorted(v) for k, v in sorted(reads.items())},
                              "unknown": sorted(unknown)}, separators=(",", ":")).encode())
