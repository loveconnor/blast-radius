"""Trace literal comparisons, simple guards and short-circuit operands on the pinned checkout.

Run in the selector image with /app on PYTHONPATH. The cold and warm passes use
different hash seeds. Use cold/warm, guard-cold/guard-warm, or reach-cold/reach-warm,
then merge the two matching traces with: merge COLD WARM OUTPUT.
"""
import ast
import builtins
import collections
import gzip
import hashlib
import functools
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import shutil
import sys
import tempfile

LIMIT = 16
UNKNOWN = ("unknown",)
values = {}
external = False
active = True
guard_sites = {}


@functools.lru_cache(maxsize=2048)
def identity_only(cls):
    if issubclass(cls, (int, str, bytes, float, complex, tuple)):
        return False
    if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
        return True
    core = sys.modules.get("sqlglot.expressions.core")
    expr = vars(core).get("Expression") if core else None
    # Expr equality checks the two concrete classes before inspecting state.
    return (expr is not None and issubclass(cls, expr)
            and cls.__eq__ is expr.__eq__ and cls.__ne__ is expr.__ne__)


def observe(site, value):
    if active:
        bucket = values.get(site)
        if bucket is None:
            bucket = values[site] = set()
        if UNKNOWN not in bucket:
            t = type(value)
            if value is None or t is bool or t is int and value.bit_length() <= 31 or t is str and len(value) <= 64:
                bucket.add((t.__name__, value))
            elif isinstance(value, int) and t.__eq__ is int.__eq__ and t.__ne__ is int.__ne__:
                if all(getattr(t, op) is getattr(int, op) for op in ("__lt__", "__le__", "__ge__", "__gt__")):
                    bucket.add(("int", int.__int__(value)) if int.bit_length(value) <= 31 else UNKNOWN)
                else:
                    bucket.add(UNKNOWN)
            elif t in (dict, list, tuple, bytes, set, frozenset) or identity_only(t):
                bucket.add(("container",))
            else:
                bucket.add(UNKNOWN)
            if UNKNOWN in bucket or len(bucket) > LIMIT:
                values[site] = {UNKNOWN}
    return value


def safe_truth(value):
    t = type(value)
    if value is None or t in (bool, int, float, str, bytes, list, tuple, dict, set, frozenset):
        return bool(value)
    # Plain instances without either truth hook are always true.
    if type(t) is type and not any("__bool__" in vars(c) or "__len__" in vars(c) for c in t.__mro__):
        return True
    raise ValueError


def safe_key(value):
    t = type(value)
    if value is None or t in (str, bytes, int, bool, float):
        return True
    return isinstance(value, int) and t.__hash__ is int.__hash__ and t.__eq__ is int.__eq__


def field(local, name):
    obj = local["self"]
    if type(obj).__getattribute__ is not object.__getattribute__:
        raise ValueError
    # Unlike getattr, this never executes a property or __getattr__.
    value = inspect.getattr_static(obj, name)
    if hasattr(type(value), "__get__"):
        raise ValueError
    return value


def observe_guard(site):
    if not active or values.get(site) == {UNKNOWN}:
        return False
    local = sys._getframe(1).f_locals
    mask = 0
    try:
        for i, spec in enumerate(guard_sites[site]):
            kind, name, *tail = spec
            if kind == "field":
                truth = safe_truth(field(local, name))
            else:
                value = local[name]
                if kind == "none":
                    truth = value is None
                elif kind == "local":
                    truth = safe_truth(value)
                else:
                    table = field(local, tail[0])
                    if type(table) not in (dict, set, frozenset, tuple) or len(table) > 256:
                        raise ValueError
                    if not safe_key(value) or not all(safe_key(k) for k in table):
                        raise ValueError
                    truth = value in table
            if truth:
                mask |= 1 << i
    except (ValueError, KeyError, AttributeError, TypeError):
        values[site] = {UNKNOWN}
    else:
        values.setdefault(site, set()).add(mask)
    return False


def mark_branch(site):
    values[site] = (True,)
    return False


def audit(event, args):
    global external
    if active and event in ("subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec"):
        external = True


def instrument(repo, dst, kind):
    from guardreads import formula
    from branchreach import parts
    files = {}
    locations = {}
    source_lines = set()
    for path in sorted((repo / "sqlglot").rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        tree = ast.parse(raw)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        edits = collections.defaultdict(list)
        for node in ast.walk(tree):
            if kind == "reach":
                children = parts(node)
                if not children:
                    continue
                # Group same-span wrappers so the enclosing one measures entry
                # before its children, without renumbering lines in SQLGlot.
                base = rel, node.lineno, node.col_offset, type(node).__name__
                for label, expr in [("parent", node), *children]:
                    site = len(locations)
                    assert (*base, label) not in locations
                    locations[(*base, label)] = site
                    a = offsets[expr.lineno - 1] + expr.col_offset
                    b = offsets[expr.end_lineno - 1] + expr.end_col_offset
                    edits[a].append((b, f"(_br_mark_branch({site}) or (".encode()))
                    edits[b].append((-a, b"))"))
                    if label != "parent":
                        source_lines.update((rel, i) for i in range(expr.lineno, expr.end_lineno + 1))
                continue
            if kind == "guard":
                if not isinstance(node, (ast.If, ast.IfExp, ast.While)):
                    continue
                left = node.test
                atoms = []
                if formula(left, atoms) is None or not 2 <= len(atoms) <= 4:
                    continue
                site = len(locations)
                guard_sites[site] = atoms
                locations[(rel, left.lineno, left.col_offset)] = site
                prefix = f"(_br_observe_guard({site}) or (".encode()
            elif not (isinstance(node, ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                    and isinstance(node.comparators[0], ast.Constant)
                    and type(node.comparators[0].value) in (type(None), str, bool, int)
                    and isinstance(node.left, (ast.Name, ast.Attribute, ast.Call, ast.Subscript))):
                continue
            else:
                left = node.left
                site = len(locations)
                locations[(rel, node.lineno, node.col_offset)] = site
                prefix = f"_br_observe_operand({site}, (".encode()
            a = offsets[left.lineno - 1] + left.col_offset
            b = offsets[left.end_lineno - 1] + left.end_col_offset
            edits[a].append((b, prefix))
            edits[b].append((-a, b"))"))
        if not edits:
            continue
        result = bytearray(raw)
        for offset, fragments in sorted(edits.items(), reverse=True):
            # Close inner spans first, then open outer spans first.
            result[offset:offset] = b"".join(piece for _, piece in sorted(fragments, reverse=True))
        compile(result, str(path), "exec", dont_inherit=True)
        new = dst / rel
        new.unlink()
        new.write_bytes(result)
        files[rel] = hashlib.sha256(raw).digest()
    return files, locations, source_lines


def run(t):
    global values, external
    values, external = {}, False
    if mode == "cold":
        runner.zygote_import({runner.module_name_for(tests[t]["file"])})
    failed = runner.run_tests([t], ids, timeouts=[300.0])
    return t, values, bool(failed or external)


def save(path, data):
    with gzip.GzipFile(path, "wb", compresslevel=6, mtime=0) as out:
        pickle.dump(data, out, protocol=4)


if __name__ == "__main__" and sys.argv[1] == "merge":
    with gzip.open(sys.argv[2], "rb") as fh:
        result = pickle.load(fh)
    with gzip.open(sys.argv[3], "rb") as fh:
        other = pickle.load(fh)
    for key in ("ids", "files", "sites"):
        assert result[key] == other[key]
    from functools import reduce
    from operator import or_
    result["both"] = {s: reduce(or_, rows.values(), 0) & reduce(or_, other["reads"].get(s, {}).values(), 0)
                      for s, rows in result["reads"].items()}
    # Only clear a test when both hash seeds and import orders reported the
    # same set of values. A warm cache can make an operand appear less varied.
    for site, reads in result["reads"].items():
        alt = other["reads"].get(site, {})
        for value in reads.keys() | alt.keys():
            result["both"][site] &= ~(reads.get(value, 0) ^ alt.get(value, 0))
    result["unknown"] |= other["unknown"]
    for site, reads in other["reads"].items():
        dest = result["reads"].setdefault(site, {})
        for value, readers in reads.items():
            dest[value] = dest.get(value, 0) | readers
    if result.get("lines"):
        result["either"] = {s: reduce(or_, rows.values(), 0) for s, rows in result.pop("reads").items()}
    save(sys.argv[4], result)
elif __name__ == "__main__":
    mode, manifest, output = sys.argv[1:]
    kind, _, mode_short = mode.rpartition("-")
    mode = mode_short
    assert mode in ("cold", "warm")
    tests = json.load(open(manifest))["tests"]
    ids = [r["id"] for r in tests]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        repo = Path("/opt/sqlglot")
        shutil.copytree(repo / "sqlglot", root / "sqlglot", copy_function=os.symlink,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (root / "tests").symlink_to(repo / "tests")
        files, sites, lines = instrument(repo, root, kind)
        os.environ["BR_REPO"] = str(root)
        os.environ["BR_WARM"] = "1" if mode == "warm" else "0"
        sys.path.insert(0, str(root))
        import runner
        builtins._br_observe_operand = observe
        builtins._br_observe_guard = observe_guard
        builtins._br_mark_branch = mark_branch
        sys.addaudithook(audit)
        reads = {}
        unknown = 0
        if mode == "warm":
            runner.zygote_import({runner.module_name_for(r["file"]) for r in tests})
            all_tests = (1 << len(ids)) - 1
            for site, seen in values.items():
                reads[site] = {v: all_tests for v in seen}
        # The source tree and maps are immutable. Each fork starts with fresh
        # test state and includes setup, teardown, and inline pool work.
        with multiprocessing.get_context("fork").Pool(4, maxtasksperchild=1) as pool:
            for t, observed, failed in pool.imap_unordered(run, range(len(ids))):
                if failed:
                    unknown |= 1 << t
                for site, seen in observed.items():
                    readers = reads.setdefault(site, {})
                    for v in seen:
                        readers[v] = readers.get(v, 0) | (1 << t)
        assert len(reads) > (100 if kind == "guard" else 500) and unknown.bit_count() < len(ids) // 8, (len(reads), unknown.bit_count())
        print(mode, len(sites), "sites", len(reads), "executed", unknown.bit_count(), "unknown tests")
        save(output, {"ids": ids, "sites": sites, "files": files, "reads": reads, "lines": lines, "unknown": unknown})
