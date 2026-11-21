"""Seed-independent structural digests of sqlglot expression trees."""
from __future__ import annotations


def canon(e, depth=0):
    import enum
    if e is None or isinstance(e, (str, int, float, bool)):
        return e
    if isinstance(e, enum.Enum):
        return ("E", e.name)
    if isinstance(e, (list, tuple)):
        return [canon(x, depth + 1) for x in e]
    if isinstance(e, dict):
        return {str(k): canon(v, depth + 1) for k, v in e.items()}
    if isinstance(e, (set, frozenset)):
        # iteration order depends on the process hash seed
        return ("S", sorted(repr(canon(x, depth + 1)) for x in e))
    args = getattr(e, "args", None)
    if isinstance(args, dict) and hasattr(e, "_meta"):
        # arg insertion order follows set iteration in places, i.e. the process hash seed
        return (type(e).__qualname__,
                sorted(((k, canon(v, depth + 1)) for k, v in args.items() if v is not None and v != []),
                       key=lambda kv: kv[0]),
                canon(e._type, depth + 1), canon(e.comments or None, depth + 1), canon(e._meta or None, depth + 1))
    r = repr(e)
    if " at 0x" in r or " object at " in r:
        r = ""   # default reprs carry an address
    return ("O", type(e).__qualname__, r)


def digest(e):
    import hashlib
    try:
        return hashlib.sha1(repr(canon(e)).encode("utf-8", "replace")).digest()
    except RecursionError:
        return None


# ---------------------------------------------------------------- exact tree snapshots
# Expression pickling goes through sqlglot.serde, which is lossy (lazy types, empty
# comments) and is itself code a patch may change. These snapshots copy the raw slots and
# rebuild a tree without calling any sqlglot code besides object allocation.
class Undumpable(Exception):
    pass


def _is_expr(e):
    return isinstance(getattr(e, "args", None), dict) and hasattr(e, "_meta") and hasattr(e, "arg_key")


def dump_tree(v):
    import enum
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, enum.Enum):
        return ("E", type(v).__module__, type(v).__qualname__, v.name)
    if _is_expr(v):
        return ("X", type(v).__module__, type(v).__qualname__, [(k, dump_tree(x)) for k, x in v.args.items()],
                dump_tree(v._type), dump_tree(v.comments), dump_tree(v._meta))
    if isinstance(v, list):
        return ("L", [dump_tree(x) for x in v])
    if isinstance(v, tuple):
        return ("U", [dump_tree(x) for x in v])
    if isinstance(v, dict):
        return ("D", [(dump_tree(k), dump_tree(x)) for k, x in v.items()])
    if isinstance(v, frozenset):
        return ("F", [dump_tree(x) for x in v])
    if isinstance(v, set):
        return ("S", [dump_tree(x) for x in v])
    raise Undumpable(type(v).__qualname__)


_CLS: dict = {}


def _cls(mod, qual):
    c = _CLS.get((mod, qual))
    if c is None:
        import importlib
        c = importlib.import_module(mod)
        for part in qual.split("."):
            c = getattr(c, part)
        _CLS[(mod, qual)] = c
    return c


def load_tree(d):
    if not isinstance(d, tuple):
        return d
    tag = d[0]
    if tag == "X":
        node = object.__new__(_cls(d[1], d[2]))
        args = {}
        for k, x in d[3]:
            v = load_tree(x)
            args[k] = v
            if type(v) is list:
                for i, c in enumerate(v):
                    if _is_expr(c):
                        c.parent, c.arg_key, c.index = node, k, i
            elif _is_expr(v):
                v.parent, v.arg_key, v.index = node, k, None
        node.args = args
        node.parent = None
        node.arg_key = None
        node.index = None
        node.comments = load_tree(d[5])
        node._type = load_tree(d[4])
        node._meta = load_tree(d[6])
        node._hash = None
        return node
    if tag == "E":
        return _cls(d[1], d[2])[d[3]]
    if tag == "L":
        return [load_tree(x) for x in d[1]]
    if tag == "U":
        return tuple(load_tree(x) for x in d[1])
    if tag == "D":
        return {load_tree(k): load_tree(x) for k, x in d[1]}
    if tag == "F":
        return frozenset(load_tree(x) for x in d[1])
    if tag == "S":
        return {load_tree(x) for x in d[1]}
    raise ValueError(tag)
