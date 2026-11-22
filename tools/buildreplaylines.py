"""Trace execution inside each recorded parser/generator phase in the actual tests.

Run inside the selector image with PYTHONPATH=/app. Capture from two fresh
interpreters with different PYTHONHASHSEEDs, then merge the captures.

    python buildreplaylines.py capture /tmp/first.pkl
    python buildreplaylines.py capture /tmp/second.pkl
    python buildreplaylines.py merge /app/replay.lines /tmp/first.pkl /tmp/second.pkl
"""
import argparse
import gzip
import json
import os
import pickle
import sys
import tempfile
import zlib
from pathlib import Path


def trace_test(t, tests, data, keys, paths, lookups):
    import replay
    import runner

    current = set()
    hits = {}
    complete = True
    outside = set()

    class Recorder:
        def append(self, item):
            nonlocal complete
            key, digest, _ = item
            index = keys.get(key)
            if index is None or digest is None or digest != data["dig"][index]:
                complete = False
            else:
                hits.setdefault(index, set()).update(current)
            current.clear()

        def extend(self, items):
            # Scaled surrogates share the union of lines reached by the group.
            snapshot = current.copy()
            for item in items:
                current.update(snapshot)
                self.append(item)

    codes = {}
    monitor = sys.monitoring
    tool = 4

    def start(code, offset):
        base = paths.get(code.co_filename)
        if base is not None:
            codes[code] = base
            monitor.set_local_events(tool, code, monitor.events.LINE)
        return monitor.DISABLE

    def line(code, number):
        if replay.PH["depth"]:
            current.add(codes[code] + number)

    replay.REC = Recorder()
    if lookups:
        import settingtrace

        def observe(key):
            if replay.PH["depth"]:
                current.add(key)
            else:
                outside.add(key)

        settingtrace.install_routes(observe)
        settingtrace.install(observe)
    else:
        monitor.use_tool_id(tool, "phase-lines")
        monitor.register_callback(tool, monitor.events.PY_START, start)
        monitor.register_callback(tool, monitor.events.LINE, line)
        monitor.set_events(tool, monitor.events.PY_START)
    failed = runner.run_tests([t], tests, timeouts=[150.0])
    if not lookups:
        monitor.set_events(tool, 0)
    if failed or not complete or replay.UNREC[0] or set(data["tests"][str(t)]) != hits.keys():
        return None
    return hits, outside


def capture(output, workers, lookups=False):
    import replay
    import runner

    app = Path(runner.__file__).resolve().parent
    repo = Path(runner.REPO)
    with gzip.open(app / "replay.json.gz", "rt") as fh:
        data = json.load(fh)
    with gzip.open(app / "map.json.gz", "rt") as fh:
        tests = json.load(fh)["tests"]
    files = sorted([repo / "sqlglot/parser.py", repo / "sqlglot/generator.py"] +
                   [p for folder in ("generators", "parsers") for p in (repo / "sqlglot" / folder).glob("*.py")])
    assert all(len(p.read_bytes().splitlines()) < 65536 for p in files)
    paths = {str(p): i << 16 for i, p in enumerate(files)}
    keys = {json.dumps(k): i for i, k in enumerate(data["keys"])}
    runner.zygote_import({runner.module_name_for(t.split("::")[0]) for t in tests})
    replay.install_recorder()
    bad = set(data["bad"])
    pending = iter(t for t in range(len(tests)) if t not in bad and str(t) in data["tests"])
    running = {}
    valid = set()
    masks = {}
    width = (len(keys) + 7) // 8
    with tempfile.TemporaryDirectory() as tmp:
        while True:
            while len(running) < workers:
                t = next(pending, None)
                if t is None:
                    break
                path = Path(tmp) / str(t)
                pid = os.fork()
                if not pid:
                    try:
                        hits = trace_test(t, tests, data, keys, paths, lookups)
                        if hits is not None:
                            path.write_bytes(zlib.compress(pickle.dumps(hits), 1))
                        os._exit(0)
                    except BaseException:
                        os._exit(1)
                running[pid] = t, path
            if not running:
                break
            pid, status = os.wait()
            t, path = running.pop(pid)
            if status or not path.exists():
                continue
            hits, outside = pickle.loads(zlib.decompress(path.read_bytes()))
            path.unlink()
            valid.add(t)
            for owner, attr in outside:
                loc = ("!outside", (owner or "*", attr or "*"))
                if loc not in masks:
                    masks[loc] = bytearray(width)
                masks[loc][t >> 3] |= 1 << (t & 7)
            for key, locations in hits.items():
                byte, bit = key >> 3, 1 << (key & 7)
                for loc in locations:
                    mask = masks.get(loc)
                    if mask is None:
                        mask = masks[loc] = bytearray(width)
                    mask[byte] |= bit
    index = {}
    for loc, mask in masks.items():
        if lookups:
            rel, line = loc
            rel, line = rel or "*", line or "*"
        else:
            file_index, line = divmod(loc, 65536)
            rel = str(files[file_index].relative_to(repo))
        index.setdefault(rel, {})[line] = zlib.compress(mask, 3)
    with output.open("wb") as fh:
        pickle.dump((valid, index), fh, protocol=4)


def merge(output, captures):
    entries = []
    for path in captures:
        with path.open("rb") as fh:
            entries.append(pickle.load(fh))
    # A test must have recreated the same complete set of baseline calls and
    # digests in every capture. Keep any path taken in any capture.
    valid = set.intersection(*(v for v, _ in entries))
    paths = {}
    sets = {}
    width = None
    for path in sorted(set.union(*(set(index) for _, index in entries))):
        tables = [index.get(path, {}) for _, index in entries]
        positions = {}
        for line in sorted(set.union(*(set(table) for table in tables))):
            bits = 0
            for table in tables:
                if line in table:
                    raw = zlib.decompress(table[line])
                    if width is None:
                        width = len(raw)
                    elif len(raw) != width:
                        raise ValueError("captures have inconsistent replay key counts")
                    bits |= int.from_bytes(raw, "little")
            packed = zlib.compress(bits.to_bytes(width, "little"), 6)
            positions[line] = sets.setdefault(packed, len(sets))
        paths[path] = positions
    if not width:
        raise ValueError("captures contain no replay keys")
    with output.open("wb") as fh:
        pickle.dump((width, valid, paths, tuple(sets)), fh, protocol=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    recording = commands.add_parser("capture")
    recording.add_argument("output", type=Path)
    recording.add_argument("--workers", type=int, default=3)
    recording.add_argument("--lookups", action="store_true", help="record concrete dispatch routes and setting reads")
    merging = commands.add_parser("merge")
    merging.add_argument("output", type=Path)
    merging.add_argument("captures", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.output, max(args.workers, 1), args.lookups)
    else:
        merge(args.output, args.captures)
