"""Baseline memo for sqlglot.optimizer.optimize (built by buildopt.py)."""
from __future__ import annotations

import enum
import hashlib
import os
import pickle
import sys
import zlib

from canon import digest, load_tree  # noqa: F401


class Unkeyable(Exception):
    pass


def _strict(e, depth=0):
    """Canonical form of a call argument; raises for anything it cannot describe exactly."""
    if depth > 400:
        raise Unkeyable()
    if e is None or isinstance(e, (str, int, float, bool)):
        return e
    if isinstance(e, enum.Enum):
        return ("E", type(e).__qualname__, e.name)
    if isinstance(e, type):
        return ("T", e.__module__, e.__qualname__)
    if isinstance(e, (list, tuple)):
        return ["L" if isinstance(e, list) else "U"] + [_strict(x, depth + 1) for x in e]
    if isinstance(e, dict):
        return ("D", sorted(((repr(_strict(k, depth + 1)), _strict(v, depth + 1)) for k, v in e.items()),
                            key=lambda kv: kv[0]))
    if isinstance(e, (set, frozenset)):
        return ("S", sorted(repr(_strict(x, depth + 1)) for x in e))
    args = getattr(e, "args", None)
    if isinstance(args, dict) and hasattr(e, "_meta") and hasattr(e, "_type"):
        return ("X", type(e).__qualname__,
                sorted(((k, _strict(v, depth + 1)) for k, v in args.items() if v is not None and v != []),
                       key=lambda kv: kv[0]),
                _strict(e._type, depth + 1), _strict(e.comments or None, depth + 1), _strict(e._meta or None, depth + 1))
    mod = type(e).__module__
    if mod.startswith("sqlglot.dialects") and hasattr(e, "settings") and hasattr(e, "version"):
        return ("DIALECT", type(e).__qualname__, _strict(tuple(e.version)), _strict(e.normalization_strategy),
                _strict(dict(e.settings)))
    if mod == "sqlglot.schema" and type(e).__qualname__ == "MappingSchema":
        d = dict(vars(e))
        return ("SCHEMA", sorted((k, _strict(v, depth + 1)) for k, v in d.items()))
    raise Unkeyable()


def call_key(expression, schema, db, catalog, dialect, sql, kwargs):
    try:
        parts = (_strict(expression), _strict(schema), _strict(db), _strict(catalog), _strict(dialect),
                 _strict(sql), _strict(kwargs))
        return hashlib.sha1(repr(parts).encode("utf-8", "replace")).digest()
    except RecursionError:
        raise Unkeyable()


def replace_everywhere(old, new):
    """Rebind every module global that is `old` (modules import optimize by name)."""
    n = 0
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == "sqlglot" or name.startswith("sqlglot.") or name.startswith("tests")):
            continue
        d = getattr(mod, "__dict__", None)
        if not d:
            continue
        for k, v in list(d.items()):
            if v is old:
                d[k] = new
                n += 1
    return n


# ---------------------------------------------------------------- run time
IDX = None
_FD = [None]
ON = [False]
AFFECTED: list = []     # contexts (0 = preamble, i + 1 = rule i) that may run a changed line
STAT = [0, 0, 0, 0]     # served from memo, short-circuited, diverged, passed through


TIMES = [0.0] * 6      # key, tree loads, digests (inside rules span), affected rules span, pass-through, after divergence
_pt = __import__("time").process_time


class _Final(BaseException):
    pass


class _Diverged(BaseException):
    def __init__(self, i, tree):
        self.i = i
        self.tree = tree


def load(prefix):
    global IDX
    import gc
    try:
        gc.disable()
        with open(prefix + ".idx", "rb") as fh:
            IDX = pickle.load(fh)
        IDX["_bin"] = prefix + ".bin"
    except Exception:  # noqa
        IDX = None
    finally:
        gc.enable()
    return IDX


def affected(lines_by_file):
    """Contexts whose recorded runs touch any of the given lines."""
    if IDX is None:
        return None
    m = 0
    for f, ls in lines_by_file.items():
        d = IDX["cov"].get(f)
        if d:
            for l in ls:
                m |= d.get(l, 0)
    return [i for i in range(len(IDX["rules"]) + 1) if (m >> i) & 1]


def _tree(d):
    if _FD[0] is None:
        _FD[0] = os.open(IDX["_bin"], os.O_RDONLY)
    off, ln = IDX["blobs"][d]
    return load_tree(pickle.loads(zlib.decompress(os.pread(_FD[0], ln, off))))


_ORIG = [None, None]    # the real optimize and its module
CH_MASK = [None]        # changed lines as a bit set over IDX["lineid"] (None: per-call filter off)
WANT = [False]          # the caller only needs to know whether the result is the baseline one


class Base:
    """Returned instead of a tree when WANT is set and the result is the recorded one."""
    __slots__ = ("digest",)

    def __init__(self, d):
        self.digest = d


def set_changed(lines_by_file):
    """Tell the stand-in which lines the patch changed (function bodies only)."""
    if IDX is None or not IDX.get("lineid") or lines_by_file is None:
        CH_MASK[0] = None
        return
    m = 0
    for f, ls in lines_by_file.items():
        d = IDX["lineid"].get(f)
        if d:
            for l in ls:
                i = d.get(l)
                if i is not None:
                    m |= 1 << i
    # a changed line that fills a process-level cache or depends on the hash seed is not
    # tied to the calls that happened to run it: no per-call judgement then
    CH_MASK[0] = None if (m & IDX.get("cold", 0)) or not IDX.get("kcov") else m


def optimize(expression, schema=None, db=None, catalog=None, dialect=None, rules=None, sql=None,
             copy=True, **kwargs):
    """Stand-in for sqlglot.optimizer.optimizer.optimize (module level: tests pickle it)."""
    want, WANT[0] = WANT[0], False
    orig, mod = _ORIG
    RULES = mod.RULES
    if rules is None:
        rules = RULES
    if not ON[0] or IDX is None or rules is not RULES or not copy:
        STAT[3] += 1
        return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
    mask = CH_MASK[0]
    if mask is None and 0 in AFFECTED:
        STAT[3] += 1
        return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
    t0 = _pt()
    try:
        key = call_key(expression, schema, db, catalog, dialect, sql, kwargs)
        st = IDX["keys"].get(key)
    except Unkeyable:
        key = st = None
    TIMES[0] += _pt() - t0
    if st is None or len(RULES) + 1 != len(st):
        STAT[3] += 1
        t0 = _pt()
        try:
            return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
        finally:
            TIMES[4] += _pt() - t0
    kc = IDX["kcov"].get(key) if mask is not None and "kcov" in IDX else None
    if kc is not None:
        # this very call, at the pinned commit: which stages ran a changed line?
        sets = IDX["ksets"]
        if sets[kc[0]] & mask:
            STAT[3] += 1
            return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
        aff = [i for i in range(len(RULES)) if sets[kc[i + 1]] & mask]
    elif 0 in AFFECTED:
        STAT[3] += 1
        return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
    else:
        aff = [c - 1 for c in AFFECTED]
    if not aff:
        STAT[0] += 1
        if want:
            return Base(st[-1])
        t0 = _pt()
        try:
            return _tree(st[-1])
        finally:
            TIMES[1] += _pt() - t0
    seq = []
    for n, i in enumerate(aff):
        nxt = aff[n + 1] if n + 1 < len(aff) else None

        def chk(expression, i=i, nxt=nxt):
            t0 = _pt()
            try:
                if digest(expression) != st[i + 1]:
                    raise _Diverged(i, expression)
            finally:
                TIMES[2] += _pt() - t0
            if nxt is None:
                raise _Final()
            t0 = _pt()
            try:
                return _tree(st[nxt])
            finally:
                TIMES[1] += _pt() - t0
        seq += [RULES[i], chk]
    t0 = _pt()
    first = _tree(st[aff[0]])
    TIMES[1] += _pt() - t0
    t0 = _pt()
    try:
        return orig(first, schema, db, catalog, dialect, tuple(seq), sql, False, **kwargs)
    except _Final:
        STAT[1] += 1
        TIMES[3] += _pt() - t0
        if want:
            return Base(st[-1])
        t0 = _pt()
        try:
            return _tree(st[-1])
        finally:
            TIMES[1] += _pt() - t0
    except _Diverged as e:
        STAT[2] += 1
        TIMES[3] += _pt() - t0
        t0 = _pt()
        try:
            return orig(e.tree, schema, db, catalog, dialect, tuple(RULES[e.i + 1:]), sql, False, **kwargs)
        finally:
            TIMES[5] += _pt() - t0


def install():
    """(Re)bind optimize in the currently imported modules to the stand-in."""
    mod = sys.modules.get("sqlglot.optimizer.optimizer")
    if mod is None or IDX is None:
        return
    cur = mod.__dict__.get("optimize")
    if cur is None or cur is optimize:
        return
    _ORIG[0], _ORIG[1] = cur, mod
    replace_everywhere(cur, optimize)
