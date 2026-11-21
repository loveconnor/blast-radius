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


class _Final(BaseException):
    pass


class _Diverged(BaseException):
    def __init__(self, i, tree):
        self.i = i
        self.tree = tree


def load(prefix):
    global IDX
    try:
        with open(prefix + ".idx", "rb") as fh:
            IDX = pickle.load(fh)
        IDX["_bin"] = prefix + ".bin"
    except Exception:  # noqa
        IDX = None
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


def optimize(expression, schema=None, db=None, catalog=None, dialect=None, rules=None, sql=None,
             copy=True, **kwargs):
    """Stand-in for sqlglot.optimizer.optimizer.optimize (module level: tests pickle it)."""
    orig, mod = _ORIG
    RULES = mod.RULES
    if rules is None:
        rules = RULES
    if not ON[0] or IDX is None or rules is not RULES or not copy or 0 in AFFECTED:
        STAT[3] += 1
        return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
    try:
        st = IDX["keys"].get(call_key(expression, schema, db, catalog, dialect, sql, kwargs))
    except Unkeyable:
        st = None
    if st is None or len(RULES) + 1 != len(st):
        STAT[3] += 1
        return orig(expression, schema, db, catalog, dialect, rules, sql, copy, **kwargs)
    aff = [c - 1 for c in AFFECTED]
    if not aff:
        STAT[0] += 1
        return _tree(st[-1])
    seq = []
    for n, i in enumerate(aff):
        nxt = aff[n + 1] if n + 1 < len(aff) else None

        def chk(expression, i=i, nxt=nxt):
            if digest(expression) != st[i + 1]:
                raise _Diverged(i, expression)
            if nxt is None:
                raise _Final()
            return _tree(st[nxt])
        seq += [RULES[i], chk]
    try:
        return orig(_tree(st[aff[0]]), schema, db, catalog, dialect, tuple(seq), sql, False, **kwargs)
    except _Final:
        STAT[1] += 1
        return _tree(st[-1])
    except _Diverged as e:
        STAT[2] += 1
        return orig(e.tree, schema, db, catalog, dialect, tuple(RULES[e.i + 1:]), sql, False, **kwargs)


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
