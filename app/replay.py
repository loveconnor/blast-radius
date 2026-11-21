"""Parse-phase record / replay.

A "phase" is one outermost call of Dialect.parse / Dialect.parse_into: tokenize + parse.
At the pinned commit every test's phase calls are recorded as (input key, output digest).
For a patch that only changes function bodies, a test that runs the changed lines only
inside such phases cannot change its outcome unless one of its recorded inputs now gives
a different output; replaying the inputs under the patch finds exactly those tests.

The output digest covers everything a caller can observe: the returned trees (classes,
args, comments, types, meta), a raised exception (type, message, structured errors) and
the log records emitted through the "sqlglot" logger.
"""
from __future__ import annotations

import enum
import hashlib
import importlib
import json
import logging
import sys
import time

import runner

PH = {"depth": 0}
REC: list = []        # (key string, digest, seconds) of the running test
UNREC = [0]           # phase calls that could not be recorded (they count as out of phase)


class Unrecordable(Exception):
    pass


def _enc(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return ["v", v]
    if isinstance(v, enum.Enum):
        return ["e", type(v).__module__, type(v).__qualname__, v.name]
    if isinstance(v, type):
        if "<locals>" in v.__qualname__:
            raise Unrecordable()
        return ["t", v.__module__, v.__qualname__]
    if isinstance(v, (list, tuple)):
        return ["l" if isinstance(v, list) else "u", [_enc(x) for x in v]]
    raise Unrecordable()


def _resolve(mod, qual):
    obj = importlib.import_module(mod)
    for part in qual.split("."):
        obj = getattr(obj, part)
    return obj


def _dec(e):
    k = e[0]
    if k == "v":
        return e[1]
    if k == "e":
        return _resolve(e[1], e[2])[e[3]]
    if k == "t":
        return _resolve(e[1], e[2])
    if k == "l":
        return [_dec(x) for x in e[1]]
    if k == "u":
        return tuple(_dec(x) for x in e[1])
    raise ValueError(k)


MAX_SQL = 20000


def shrink(sql, opts):
    """Scaled-down stand-ins for a huge input that is one unit repeated n times
    ("SELECT u,u,u,..."): the same unit repeated far fewer times, with a max_nodes
    limit scaled by the same ratio. Returns [(sql, opts)] or [] if it has no such shape."""
    parts = sql.split(",")
    n = len(parts)
    if n < 2000:
        return []
    unit = parts[1]
    if not unit or any(p != unit for p in parts[1:]) or not parts[0].endswith(unit):
        return []
    head = parts[0][: len(parts[0]) - len(unit)]
    out = []
    for m in (100, 400):
        o = dict(opts)
        mn = o.get("max_nodes")
        if isinstance(mn, int) and not isinstance(mn, bool) and mn > 0:
            o["max_nodes"] = max(1, mn * m // n)
        out.append((head + ",".join([unit] * m), o))
    return out


def make_key(kind, dialect, sql, opts, into=None, long_ok=False):
    cls = type(dialect)
    if "<locals>" in cls.__qualname__ or not isinstance(sql, str) or (len(sql) > MAX_SQL and not long_ok):
        raise Unrecordable()
    settings = {}
    for k, v in dialect.settings.items():
        if not (v is None or isinstance(v, (bool, int, float, str))):
            raise Unrecordable()
        settings[k] = v
    version = list(dialect.version)
    if version == [sys.maxsize, 0, 0]:
        version = None
    ns = dialect.normalization_strategy
    norm = None if ns is cls.NORMALIZATION_STRATEGY else ns.value
    return [kind, cls.__module__, cls.__qualname__, version, norm, settings, sql,
            [[k, _enc(v)] for k, v in sorted(opts.items())], None if into is None else _enc(into)]


_DIALECTS: dict = {}


def _dialect(key):
    d = json.dumps(key[1:6], sort_keys=True)
    inst = _DIALECTS.get(d)
    if inst is None:
        cls = _resolve(key[1], key[2])
        kw = dict(key[5])
        if key[3] is not None:
            kw["version"] = ".".join(str(x) for x in key[3])
        if key[4] is not None:
            kw["normalization_strategy"] = key[4]
        inst = _DIALECTS[d] = cls(**kw)
    return inst


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(0)
        self.records = []

    def emit(self, record):
        try:
            self.records.append((record.levelname, record.getMessage()))
        except Exception:  # noqa
            self.records.append((record.levelname, "?"))


_LOGGER = logging.getLogger("sqlglot")


def observe(fn):
    """Run fn() and digest everything observable about it. Returns (digest, result, exc)."""
    h = _Logs()
    _LOGGER.addHandler(h)
    res = exc = None
    try:
        res = fn()
        out = ("OK", runner.canon(res))
    except RecursionError as e:
        exc = e
        out = ("REC",)
    except Exception as e:  # noqa
        exc = e
        # several errors are reported in set-iteration order: compare them as a bag
        errs = getattr(e, "errors", None)
        out = ("EXC", type(e).__qualname__, sorted(str(e).split("\n\n")),
               sorted(repr(runner.canon(x)) for x in errs) if isinstance(errs, list) else runner.canon(errs))
    finally:
        _LOGGER.removeHandler(h)
    try:
        dig = hashlib.sha1(repr((out, h.records)).encode("utf-8", "replace")).hexdigest()[:16]
    except RecursionError:
        dig = None
    return dig, res, exc


def run_key(key):
    """Replay one recorded phase call under the current code; returns its digest."""
    d = _dialect(key)
    opts = {k: _dec(v) for k, v in key[7]}
    if key[0] == "p":
        return observe(lambda: d.parse(key[6], **opts))[0]
    return observe(lambda: d.parse_into(_dec(key[8]), key[6], **opts))[0]


def install_recorder():
    """Build time: wrap the phase entry points to record their calls."""
    from sqlglot.dialects.dialect import Dialect
    orig_parse, orig_into = Dialect.parse, Dialect.parse_into

    def _surrogates(self, sql, opts):
        """Record scaled-down stand-ins for a huge repetitive input. True if recorded."""
        try:
            small = shrink(sql, opts)
            if not small:
                return False
            recs = []
            for ssql, sopts in small:
                key = json.dumps(make_key("p", self, ssql, sopts, long_ok=True))
                PH["depth"] += 1
                t = time.perf_counter()
                try:
                    dig = observe(lambda: orig_parse(self, ssql, **sopts))[0]
                finally:
                    PH["depth"] -= 1
                recs.append((key, dig, time.perf_counter() - t))
            REC.extend(recs)
            return True
        except Exception:  # noqa
            return False

    def _phase(key_fn, call, big=None):
        if PH["depth"]:
            return call()
        try:
            key = json.dumps(key_fn())
        except Exception:  # noqa
            if big is None or not _surrogates(*big):
                UNREC[0] += 1
                return call()
            # the real call still runs as a phase; the stand-ins are what gets replayed
            PH["depth"] += 1
            try:
                return call()
            finally:
                PH["depth"] -= 1
        PH["depth"] += 1
        t = time.perf_counter()
        try:
            dig, res, exc = observe(call)
        finally:
            PH["depth"] -= 1
        REC.append((key, dig, time.perf_counter() - t))
        if exc is not None:
            raise exc
        return res

    def parse(self, sql, **opts):
        big = (self, sql, opts) if isinstance(sql, str) and len(sql) > MAX_SQL else None
        return _phase(lambda: make_key("p", self, sql, opts), lambda: orig_parse(self, sql, **opts), big)

    def parse_into(self, expression_type, sql, **opts):
        return _phase(lambda: make_key("i", self, sql, opts, expression_type),
                      lambda: orig_into(self, expression_type, sql, **opts))

    Dialect.parse = parse
    Dialect.parse_into = parse_into
