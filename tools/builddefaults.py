"""Trace explicit versus omitted SQLGlot method arguments on the pinned test suite.

Run inside the native entry image with tests.json and the output path as arguments.
Pass a previous trace as a third argument to retain only observations verified in both
interpreters. The checked-in map combines two runs with different hash seeds.
Each test runs in a fork. A marker in a function's defaults identifies omitted arguments
at Python frame entry, before the first instruction. Restore the original value in the
frame immediately. Discard cases that inspect defaults or escape the traced interpreter.
"""
import gc
import gzip
import inspect
import json
import linecache
import multiprocessing
import os
import sys
import types

sys.path.insert(0, "/app")
import runner
from selcore import Map

tests = [t["id"] for t in json.load(open(sys.argv[1]))["tests"]]
coverage = Map("/app/map.json.gz")
assert tests == coverage.tests
runner.zygote_import({runner.module_name_for(t.split("::")[0]) for t in tests})
root = os.path.realpath(runner.REPO) + "/"
prefix = root + "sqlglot/"
entries = {}
eligible = set()
for module_name, module in tuple(sys.modules.items()):
    if not (module_name == "sqlglot" or module_name.startswith("sqlglot.")):
        continue
    for cls in tuple(vars(module).values()) if isinstance(module, types.ModuleType) else ():
        if not isinstance(cls, type) or cls.__module__ != module_name:
            continue
        for method in vars(cls).values():
            if not isinstance(method, types.FunctionType):
                continue
            fn = inspect.unwrap(method)
            if not isinstance(fn, types.FunctionType):
                continue
            co = fn.__code__
            if co in entries or not co.co_filename.startswith(prefix) or fn.__qualname__ != co.co_qualname:
                continue
            if co.co_flags & (inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
                continue
            defaults = fn.__defaults__ or ()
            start = co.co_argcount - len(defaults)
            args = dict(zip(co.co_varnames[start:co.co_argcount], defaults))
            args.update(fn.__kwdefaults__ or {})
            args = {k: v for k, v in args.items() if type(v) in (bool, int, str, type(None))}
            if not args:
                continue
            rel = co.co_filename[len(root):]
            lines = [n for _, _, n in co.co_lines() if n]
            reached = coverage.covering(rel, min(lines), max(lines))
            import_lines = coverage.importlines.get(rel, set())
            if not reached or import_lines.intersection(lines[1:]) - {co.co_firstlineno}:
                continue
            entries[co] = (fn, args)
            eligible.update(reached)

watched = {}
markers = {}
for co, (fn, args) in entries.items():
    originals = {}
    for name, value in args.items():
        marker = object()
        key = f"{co.co_filename[len(root):]}:{co.co_qualname}:{name}"
        originals[name] = (marker, value, key)
    watched[fn] = [key for _, _, key in originals.values()]
    markers[co] = originals
    defaults = fn.__defaults__
    if defaults:
        names = co.co_varnames[co.co_argcount - len(defaults):co.co_argcount]
        fn.__defaults__ = tuple(originals[n][0] if n in originals else v for n, v in zip(names, defaults))
    if fn.__kwdefaults__:
        fn.__kwdefaults__ = {n: originals[n][0] if n in originals else v for n, v in fn.__kwdefaults__.items()}

mon = sys.monitoring
tool_id = 3
mon.use_tool_id(tool_id, "default-arguments")
seen = set()
used = set()
sites = {}
site_cache = {}
escaped = False


def caller_site(frame, method):
    # Skip forwarding decorators. Keep the real caller's executed line only when
    # the source directly names this method. Unresolved calls retain full coverage.
    while frame and frame.f_code.co_filename.startswith(prefix):
        loc = (frame.f_code.co_filename, frame.f_lineno, method)
        if loc not in site_cache:
            site_cache[loc] = "." + method + "(" in linecache.getline(loc[0], loc[1])
        if site_cache[loc]:
            return loc[0][len(root):], loc[1]
        if "<locals>" not in frame.f_code.co_qualname:
            break
        frame = frame.f_back
    return None


def enter(code, offset):
    frame = sys._getframe(1)
    local = frame.f_locals
    for name, (marker, value, key) in markers[code].items():
        seen.add(key)
        if local[name] is marker:
            used.add(key)
            sites.setdefault(key, set()).add(caller_site(frame.f_back, code.co_name))
            local[name] = value


def audit(event, args):
    global escaped
    if event in ("os.fork", "os.posix_spawn", "subprocess.Popen"):
        escaped = True
    elif event == "exec" and args[0].co_name == "<module>" and args[0].co_filename.startswith(prefix):
        escaped = True
    elif event in ("object.__getattr__", "object.__setattr__", "object.__delattr__"):
        if len(args) >= 2 and args[1] in ("__defaults__", "__kwdefaults__", "__code__") and type(args[0]) is types.FunctionType:
            if event != "object.__getattr__" or args[1] != "__code__":
                for key in watched.get(args[0], ()):
                    used.add(key)
                    sites.setdefault(key, set()).add(None)


def trace(t):
    global escaped
    sites.clear()
    seen.clear()
    used.clear()
    mon.register_callback(tool_id, mon.events.PY_START, enter)
    for code in entries:
        mon.set_local_events(tool_id, code, mon.events.PY_START)
    sys.addaudithook(audit)
    escaped = False
    failed = runner.run_tests([t], tests, timeouts=[90.0])
    if failed or escaped:
        return t, [], {}
    return t, sorted(seen - used), {k: sorted(v) for k, v in sites.items() if None not in v}


if __name__ == "__main__":
    records = {}
    gc.freeze()
    with multiprocessing.get_context("fork").Pool(3, maxtasksperchild=1) as pool:
        for t, keys, locations in pool.imap_unordered(trace, sorted(eligible)):
            for k in keys:
                records.setdefault(k, {"explicit": [], "sites": {}})["explicit"].append(t)
            for k, locs in locations.items():
                records.setdefault(k, {"explicit": [], "sites": {}})["sites"][t] = locs
    if len(sys.argv) > 3:
        with gzip.open(sys.argv[3], "rt") as fh:
            previous = json.load(fh)
        for key, row in records.items():
            old = previous.get(key, {})
            old_sites = {t: sites for sites, ids in old.get("groups", []) for t in ids}
            old_explicit = set(old.get("explicit", ()))
            new_explicit = set(row["explicit"])
            both = (old_explicit | old_sites.keys()) & (new_explicit | row["sites"].keys())
            row["explicit"] = sorted(old_explicit & new_explicit)
            row["sites"] = {
                t: sorted(set(map(tuple, old_sites.get(t, ()))) | set(map(tuple, row["sites"].get(t, ()))))
                for t in both.difference(row["explicit"])
            }
    for row in records.values():
        groups = {}
        for t, sites in row.pop("sites").items():
            groups.setdefault(tuple(map(tuple, sites)), []).append(t)
        row["groups"] = [(sites, sorted(ids)) for sites, ids in sorted(groups.items())]
        row["explicit"].sort()
    with gzip.open(sys.argv[2], "wt") as out:
        json.dump(records, out, sort_keys=True, separators=(",", ":"))
    print(len(entries), "methods,", len(eligible), "tests,", sum(len(v["explicit"]) for v in records.values()), "explicit-argument records")
