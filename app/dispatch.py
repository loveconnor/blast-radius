"""Reach of the Python SQL generator's cached expression-to-handler table."""
import ast
import __future__
import functools
import gzip
import json
import sys
from pathlib import Path


def name(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


@functools.lru_cache(maxsize=1)
def trace():
    with gzip.open(Path(__file__).with_name("dispatch.json.gz"), "rt") as fh:
        return json.load(fh)


def guard_only(before, after):
    """Accept changes only to pure tests inside the pinned dispatch builder."""
    old, new = ast.parse(before), ast.parse(after)
    def builder(tree):
        return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_dispatch")
    orig, edited = builder(old), builder(new)
    checks = [n for n in ast.walk(edited) if isinstance(n, ast.If)]
    base = [n for n in ast.walk(orig) if isinstance(n, ast.If)]
    if len(checks) != len(base):
        return None
    code = compile(ast.Module(body=[edited], type_ignores=[]), "<dispatch>", "exec", dont_inherit=True,
                   flags=__future__.annotations.compiler_flag)
    for current, previous in zip(checks, base):
        if ast.dump(current.test) == ast.dump(previous.test):
            continue
        for node in ast.walk(current.test):
            if isinstance(node, ast.Name):
                if node.id not in {"attr_name", "expr_cls", "expr_key", "dispatch"}:
                    return None
            elif isinstance(node, ast.Call):
                if not (isinstance(node.func, ast.Attribute) and not node.keywords and len(node.args) == 1
                        and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str):
                    return None
            elif isinstance(node, ast.Attribute):
                if not (isinstance(node.value, ast.Name) and node.value.id == "attr_name"
                        and node.attr in ("startswith", "endswith")):
                    return None
            elif not isinstance(node, (ast.Load, ast.Constant, ast.Compare, ast.BoolOp, ast.UnaryOp,
                                       ast.Not, ast.And, ast.Or, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
                return None
        current.test = previous.test
    return code if ast.dump(old) == ast.dump(new) else None


def generator_classes():
    from sqlglot.executor.python import Python
    from sqlglot.generator import Generator
    from sqlglot.dialects.dialect import Dialect
    return {d.generator_class for d in Dialect.classes.values()} | {Generator, Python.generator_class}


def guard_reach(decision, coverage, repo):
    path = "sqlglot/generator.py"
    if set(decision.files) != {path} or decision.reasons != [f"cache-fill {path}"]:
        return None
    try:
        code = guard_only((repo / path).read_text(), decision.files[path])
        if code is None:
            return None
        data = trace()
        if data["ids"] != coverage.tests:
            return None
        # Importing SQLGlot here would change the main worker's pristine module
        # state before it prepares unrelated patches. Compare in an isolated fork.
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        reader, writer = ctx.Pipe(duplex=False)
        child = ctx.Process(target=_compare, args=(writer, code, repo, data, coverage))
        child.start()
        writer.close()
        try:
            return reader.recv() if reader.poll(5) else None
        finally:
            reader.close()
            if child.is_alive():
                child.terminate()
            child.join()
    except Exception:
        return None


def _compare(writer, code, repo, data, coverage):
    try:
        sys.path.insert(0, str(repo))
        import sqlglot.generator as gen
        classes = generator_classes()
        if set(map(name, classes)) != set(data["classes"]):
            return None
        ns = vars(gen).copy()
        exec(code, ns)
        reached = set(data["unknown"]) | coverage.covering("tests/dialects/test_dialect.py", 237, 237)
        for cls in classes:
            old, new = gen._build_dispatch(cls), ns["_build_dispatch"](cls)
            reads = data["reads"].get(name(cls), {})
            for expr in old.keys() | new.keys():
                if old.get(expr) is not new.get(expr):
                    reached.update(reads.get(name(expr), ()))
        # This test warms each builtin generator before measuring repeat
        # construction. The edited guard only runs on the first cache fill.
        reached.difference_update(i for i in reached.copy()
                                  if data["ids"][i] == "tests/test_generator.py::TestGenerator::test_generator_memory")
        writer.send(sorted(reached))
    finally:
        writer.close()


def transform_reach(delta, coverage):
    """Concrete lookups of edited TRANSFORMS keys, including inherited tables."""
    if not delta or any(kind != "cls" or attr != "TRANSFORMS" or not isinstance(key, str)
                        for kind, owner, attr, key in delta):
        return None
    try:
        data = trace()
        if coverage.tests != data["ids"]:
            return None
        classes = generator_classes()
        if set(map(name, classes)) != set(data["classes"]):
            return None
        direct = coverage.covering("tests/dialects/test_dialect.py", 237, 237)
        observed = set(data["unknown"])
        for _, owner, _, key in delta:
            if not key.startswith("sqlglot.expressions."):
                return None
            for cls in classes:
                if owner not in {name(base) for base in cls.__mro__}:
                    continue
                for expr, tests in data["reads"].get(name(cls), {}).items():
                    if key == expr.replace(":", "."):
                        observed.update(tests)
        # These paths read TRANSFORMS directly, bypassing Generator.sql.
        for path, line in (("sqlglot/transforms.py", 52), ("sqlglot/generators/postgres.py", 601),
                           ("sqlglot/generator.py", 4100)):
            direct.update(coverage.covering(path, line, line))
        return observed, direct
    except Exception:
        return None
