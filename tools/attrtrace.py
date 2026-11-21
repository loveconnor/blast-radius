"""Build-time: record, per test, which sqlglot class attributes (and dict keys) it reads.

Every attribute in the __dict__ of every class defined by sqlglot is replaced by a
recording descriptor that returns the original value (delegating to the value's own
__get__ for functions, properties etc.). Dict values become a dict subclass that
records the keys looked up (iteration records "*").

Each test runs in its own fork of an instrumented zygote, so lazily computed caches
are rebuilt by every test and their inputs show up in every test that needs them.

usage: python attrtrace.py <out.json> <parallel>
"""
import enum
import importlib
import json
import os
import pkgutil
import select
import sys
import time
import types

sys.path.insert(0, "/w/tools")
os.environ.setdefault("BR_REPO", "/w/sqlglot")
import runner  # noqa: E402
from statediff import _Cmp, _classes_of  # noqa: E402

OUT = sys.argv[1]
PAR = int(sys.argv[2])
tests = json.load(open("/kit/sample/tests.json"))["tests"]
nodeids = [t["id"] for t in tests]

runner.zygote_import({runner.module_name_for(t["file"]) for t in tests})
import sqlglot  # noqa: E402

READS = set()
_key = _Cmp().key
PHASE = os.environ.get("BR_PHASE") == "1"   # record only reads made outside parse phases
import replay  # noqa: E402
_PH = replay.PH


def keystr(k):
    k = _key(k)
    if isinstance(k, tuple) and len(k) == 3 and k[0] in ("E", "T", "O"):
        return f"{k[1]}.{k[2]}"
    return repr(k) if not isinstance(k, str) else k


class TDict(dict):
    __slots__ = ("_br_owner",)

    def _rec(self, k):
        if PHASE and _PH["depth"]:
            return
        try:
            READS.add((self._br_owner[0], self._br_owner[1], keystr(k)))
        except Exception:  # noqa
            READS.add((self._br_owner[0], self._br_owner[1], "*"))

    def _all(self):
        if PHASE and _PH["depth"]:
            return
        READS.add((self._br_owner[0], self._br_owner[1], "*"))

    def __getitem__(self, k):
        self._rec(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._rec(k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        self._rec(k)
        return dict.__contains__(self, k)

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def __len__(self):
        self._all()
        return dict.__len__(self)

    def __eq__(self, other):
        self._all()
        return dict.__eq__(self, other)

    __hash__ = None

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def __or__(self, other):
        self._all()
        return dict(dict.items(self)) | other

    def __ror__(self, other):
        self._all()
        return other | dict(dict.items(self))

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def __reduce__(self):
        self._all()
        return (dict, (dict(dict.items(self)),))


class Tracker:
    __slots__ = ("owner", "attr", "value", "getter")

    def __init__(self, owner, attr, value):
        self.owner = owner
        self.attr = attr
        self.value = value
        self.getter = getattr(type(value), "__get__", None)

    def __get__(self, obj, objtype=None):
        if not (PHASE and _PH["depth"]):
            READS.add((self.owner, self.attr, None))
        if self.getter is not None:
            return self.getter(self.value, obj, objtype)
        return self.value


class DataTracker(Tracker):
    __slots__ = ()

    def __set__(self, obj, val):
        type(self.value).__set__(self.value, obj, val)

    def __delete__(self, obj):
        type(self.value).__delete__(self.value, obj)


SKIP_TYPES = (types.MemberDescriptorType, types.GetSetDescriptorType, types.WrapperDescriptorType,
              types.MethodDescriptorType, types.ClassMethodDescriptorType, type)


def instrument():
    mods = [m for n, m in list(sys.modules.items()) if (n == "sqlglot" or n.startswith("sqlglot.")) and m]
    n = 0
    for mod in mods:
        for C in _classes_of(mod):
            if issubclass(C, enum.Enum) or issubclass(C, BaseException):
                continue
            owner = f"{mod.__name__}:{C.__qualname__}"
            for k, v in list(vars(C).items()):
                if k.startswith("__") and k.endswith("__"):
                    continue
                if isinstance(v, SKIP_TYPES) or k == "_abc_impl":
                    continue
                if type(v) is dict:
                    td = TDict(v)
                    td._br_owner = (owner, k)
                    v = td
                try:
                    if hasattr(type(v), "__set__"):
                        setattr(C, k, DataTracker(owner, k, v))
                    else:
                        setattr(C, k, Tracker(owner, k, v))
                    n += 1
                except (TypeError, AttributeError):
                    pass
    return n


# import every module first so that every class exists before instrumenting
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not m.name.endswith("__main__"):
        try:
            importlib.import_module(m.name)
        except Exception:  # noqa
            pass
print("instrumented", instrument(), file=sys.stderr, flush=True)
if PHASE:
    replay.install_recorder()

results = {}
ONLY = os.environ.get("ONLY")
todo = [int(x) for x in ONLY.split(",")] if ONLY else list(range(len(tests)))
if os.environ.get("TRACE_ONLY"):
    todo = [int(x) for x in os.environ["TRACE_ONLY"].split(",")]
if ONLY:
    import traceback, unittest
    for t in todo:
        p = nodeids[t].split("::")
        C = getattr(importlib.import_module(runner.module_name_for(p[0])), p[1])
        r = unittest.TestResult(); C(p[2]).run(r)
        for _, tb in r.errors + r.failures:
            print(nodeids[t], tb[-1500:], file=sys.stderr)
    sys.exit(0)
running = {}
t0 = time.time()
while todo or running:
    while todo and len(running) < PAR:
        t = todo.pop(0)
        rd, wr = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rd)
            READS.clear()
            f = runner.run_tests([t], nodeids, timeouts=[3000.0])
            out = {"t": t, "failed": bool(f), "reads": [list(r) for r in READS]}
            data = json.dumps(out).encode()
            while data:
                k = os.write(wr, data)
                data = data[k:]
            os._exit(0)
        os.close(wr)
        running[rd] = (pid, t, [])
    r, _, _ = select.select(list(running), [], [], 5)
    for fd in r:
        pid, t, buf = running[fd]
        ch = os.read(fd, 1 << 20)
        if ch:
            buf.append(ch)
            continue
        os.close(fd)
        os.waitpid(pid, 0)
        del running[fd]
        try:
            results[t] = json.loads(b"".join(buf))
        except Exception:  # noqa
            results[t] = {"t": t, "failed": True, "reads": None}
        if len(results) % 100 == 0:
            print(len(results), round(time.time() - t0), file=sys.stderr, flush=True)
json.dump({str(k): v for k, v in results.items()}, open(OUT, "w"))
bad = [nodeids[t] for t, v in results.items() if v["failed"]]
print("failed under instrumentation:", len(bad), bad[:20], file=sys.stderr)
