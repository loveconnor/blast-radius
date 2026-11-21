"""pytest plugin: per-test line coverage (sys.monitoring) + files opened, written as JSON."""
import builtins, io, json, os, sys, threading
import pytest

SRC = os.environ.get("COV_SRC", "/src")
OUT = os.environ["COV_OUT"]
import concurrent.futures as _cf
class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        import pickle
        if initializer: initializer(*pickle.loads(pickle.dumps(initargs)))
    def submit(self, fn, *a, **k):
        f = _cf.Future()
        import pickle
        base = cur["key"]
        cnt = _subcount.setdefault(base, 0); _subcount[base] = cnt + 1
        if os.environ.get("COV_SUBMIT"):
            _switch(base + "#" + str(cnt))
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e: f.set_exception(e)
        if os.environ.get("COV_SUBMIT"):
            _switch(base)
        return f
    def map(self, fn, *its, **kw): return map(fn, *its)
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
_cf.ProcessPoolExecutor = InlineExecutor
_subcount = {}
mon = sys.monitoring
TOOL = 3
cur = {"key": "__import__"}
data = {}       # key -> {file: set(lines)}
opened = {}     # key -> set(paths)

def _line(code, line):
    fn = code.co_filename
    if not fn.startswith(SRC):
        return mon.DISABLE
    d = data.setdefault(cur["key"], {})
    s = d.get(fn)
    if s is None:
        s = d[fn] = set()
    s.add(line)
    # don't DISABLE: we need re-firing per test. Use restart_events between tests instead.
    return mon.DISABLE

mon.use_tool_id(TOOL, "covmap")
mon.register_callback(TOOL, mon.events.LINE, _line)
mon.set_events(TOOL, mon.events.LINE)

_orig_open = builtins.open
def _open(file, *a, **k):
    try:
        p = os.fspath(file)
        if isinstance(p, str) and p.startswith(SRC):
            opened.setdefault(cur["key"], set()).add(os.path.realpath(p))
    except TypeError:
        pass
    return _orig_open(file, *a, **k)
builtins.open = _open
io.open = _open
_orig_listdir = os.listdir
def _listdir(p="."):
    try:
        ps = os.fspath(p)
        if isinstance(ps, str) and ps.startswith(SRC):
            opened.setdefault(cur["key"], set()).add(os.path.realpath(ps) + "/")
    except TypeError:
        pass
    return _orig_listdir(p)
os.listdir = _listdir

def _switch(key):
    cur["key"] = key
    mon.restart_events()

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _switch(item.nodeid + "|setup")

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    _switch(item.nodeid)

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    _switch(item.nodeid + "|teardown")

def pytest_sessionfinish(session):
    _switch("__end__")
    out = {k: {f: sorted(v) for f, v in d.items()} for k, d in data.items()}
    with _orig_open(OUT, "w") as fh:
        json.dump({"lines": out, "opened": {k: sorted(v) for k, v in opened.items()}}, fh)
