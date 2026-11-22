"""Trace literal comparisons, simple guards and short-circuit operands on the pinned checkout.

Run in the selector image with /app on PYTHONPATH. The cold and warm passes use
different hash seeds. Use cold/warm, guard-cold/guard-warm, reach-cold/reach-warm, or index-cold/index-warm,
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
import types

LIMIT = 16
UNKNOWN = ("unknown",)
values = {}
external = False
active = True
guard_sites = {}
index_sites = {}
parser_match = None
importing = True


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


@functools.lru_cache(maxsize=1024)
def ordinary_instance(cls):
    return (type(cls) is type and cls.__getattribute__ is object.__getattribute__
            and inspect.getattr_static(cls, "__class__") is vars(object)["__class__"])


def guard_call(spec, frame):
    global parser_match
    kind, name, attr = spec
    value = frame.f_locals[name]
    if kind == "instance":
        module = frame.f_globals.get("exp")
        if (module is not sys.modules.get("sqlglot.expressions") or not isinstance(module, types.ModuleType)
                or frame.f_globals.get("isinstance", builtins.isinstance) is not builtins.isinstance):
            raise ValueError
        target = vars(module).get(attr)
        if type(target) is not type or not ordinary_instance(type(value)):
            raise ValueError
        return target in type(value).__mro__
    if parser_match is None and importing:
        parser_match = save_parser_match()
    if parser_match is None or type(value).__getattribute__ is not object.__getattribute__:
        raise ValueError
    fn, code, parser, token, token_type = parser_match
    if (inspect.getattr_static(value, "_match") is not fn or fn.__code__ is not code
            or frame.f_globals.get("TokenType") is not token_type or token_type.__eq__ is not int.__eq__
            or inspect.getattr_static(value, "_curr") is not vars(parser)["_curr"]):
        raise ValueError
    curr = object.__getattribute__(value, "_curr")
    if type(curr) is not token or type(vars(token).get("token_type")) is not types.MemberDescriptorType:
        raise ValueError
    actual = object.__getattribute__(curr, "token_type")
    expected = vars(token_type).get(attr)
    if type(actual) is not token_type or type(expected) is not token_type or actual is expected:
        # On matching input _match also calls _add_comments. Leave that
        # execution to verification instead of assuming the method is pure.
        raise ValueError
    return False


def save_parser_match():
    mod = sys.modules.get("sqlglot.parser")
    if mod is None:
        return None
    parser = vars(mod).get("Parser")
    if parser is None:
        return None
    fn = vars(parser).get("_match")
    if (type(fn) is not types.FunctionType or fn.__qualname__ != "Parser._match"
            or fn.__code__.co_filename != mod.__file__):
        return None
    return fn, fn.__code__, parser, vars(mod)["Token"], vars(mod)["TokenType"]


def observe_guard(site):
    if not active or values.get(site) == {UNKNOWN}:
        return False
    frame = sys._getframe(1)
    local = frame.f_locals
    mask = 0
    try:
        for i, spec in enumerate(guard_sites[site]):
            kind, name, *tail = spec
            if kind in ("instance", "peek"):
                truth = guard_call(spec, frame)
            elif kind == "field":
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


def observe_index(site, sequence):
    from indexreads import INDICES
    index = index_sites[site]
    mask = 0
    if type(sequence) in (list, tuple) and -len(sequence) <= index < len(sequence):
        value = sequence[index]
        for i, alternate in enumerate(INDICES):
            if -len(sequence) <= alternate < len(sequence) and sequence[alternate] is value:
                mask |= 1 << i
    values.setdefault(site, set()).add(mask)
    return sequence


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
    if kind == "index":
        from indexreads import INDICES, literal
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
            if kind == "index":
                if not (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                        and literal(node.slice) in INDICES):
                    continue
                site = len(locations)
                index_sites[site] = literal(node.slice)
                locations[(rel, node.lineno, node.col_offset)] = site
                left = node.value
                prefix = f"_br_observe_index({site}, (".encode()
            elif kind == "guard":
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
    global values, external, parser_match, importing
    values, external = {}, False
    if mode == "cold":
        runner.zygote_import({runner.module_name_for(tests[t]["file"])})
    imports = {}
    if kind == "guard":
        parser_match = save_parser_match()
        imports, values = values, {}
    importing = False
    failed = runner.run_tests([t], ids, timeouts=[300.0])
    return t, values, imports, bool(failed or external)


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
    def stable_masks(first, second):
        both = {site: reduce(or_, rows.values(), 0) & reduce(or_, second.get(site, {}).values(), 0)
                for site, rows in first.items()}
        for site, rows in first.items():
            alt = second.get(site, {})
            for value in rows.keys() | alt.keys():
                both[site] &= ~(rows.get(value, 0) ^ alt.get(value, 0))
        return both

    result["both"] = stable_masks(result["reads"], other["reads"])
    if "body_reads" in result and "body_reads" in other:
        # A warm interpreter runs all module initializers together. Their
        # extra observations need not make a repeated test body unstable.
        # Keep every import observation in the outcome checks below.
        stable = stable_masks(result.pop("body_reads"), other.pop("body_reads"))
        for site, bits in stable.items():
            result["both"][site] = result["both"].get(site, 0) | bits
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
        builtins._br_observe_index = observe_index
        builtins._br_mark_branch = mark_branch
        sys.addaudithook(audit)
        reads, body_reads = {}, {}
        unknown = 0
        if mode == "warm":
            runner.zygote_import({runner.module_name_for(r["file"]) for r in tests})
            all_tests = (1 << len(ids)) - 1
            for site, seen in values.items():
                reads[site] = {v: all_tests for v in seen}
        # The source tree and maps are immutable. Each fork starts with fresh
        # test state and includes setup, teardown, and inline pool work.
        with multiprocessing.get_context("fork").Pool(4, maxtasksperchild=1) as pool:
            for t, observed, imports, failed in pool.imap_unordered(run, range(len(ids))):
                if failed:
                    unknown |= 1 << t
                sources = [(observed, reads)]
                if kind == "guard":
                    sources.extend(((imports, reads), (observed, body_reads)))
                for observations, output_reads in sources:
                    for site, seen in observations.items():
                        readers = output_reads.setdefault(site, {})
                        for v in seen:
                            readers[v] = readers.get(v, 0) | (1 << t)
        assert sites and len(reads) * 2 >= len(sites), (len(reads), len(sites))
        assert unknown.bit_count() < len(ids) // 8, unknown.bit_count()
        print(mode, len(sites), "sites", len(reads), "executed", unknown.bit_count(), "unknown tests")
        record = {"ids": ids, "sites": sites, "files": files, "reads": reads, "lines": lines, "unknown": unknown}
        if kind == "guard":
            record["body_reads"] = body_reads
        save(output, record)
