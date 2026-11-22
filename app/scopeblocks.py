"""Skip independent, unchanged union-chain checks in the pinned scope test."""
import ast
import functools
import hashlib
import inspect
import pickle
import textwrap
from pathlib import Path

TEST = "tests/test_optimizer.py::TestOptimizer::test_scope"
INDEX = Path(__file__).with_suffix(".idx")


def source_blocks(method, source=None):
    if source is None:
        source = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(source)
    body = tree.body[0].body
    starts = {}
    for i, stmt in enumerate(body):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            starts.setdefault(stmt.targets[0].id, i)
    groups = [body[starts["many_unions"]:starts["chain"]], body[starts["chain"]:starts["root"]]]
    return source, tree, groups


@functools.lru_cache(maxsize=1)
def index():
    try:
        with INDEX.open("rb") as fh:
            return pickle.load(fh)
    except FileNotFoundError:
        return None


def unaffected(lines, attrs=()):
    data = index()
    if not data or not set(attrs) <= data["tracked"]:
        return ()
    skip = []
    for i, (coverage, reads) in enumerate(data["blocks"]):
        if not reads.intersection(attrs) and not any(
            coverage.get(path, set()).intersection(numbers) for path, numbers in lines.items()
        ):
            skip.append(i)
    return tuple(skip)


def apply(case, skip):
    method = type(case).test_scope
    source = textwrap.dedent(inspect.getsource(method))
    if hashlib.sha256(source.encode()).digest() != index()["source"]:
        return
    _, tree, groups = source_blocks(method, source)
    omit = {stmt for i in skip for stmt in groups[i]}
    fn = tree.body[0]
    fn.body = [stmt for stmt in fn.body if stmt not in omit]
    namespace = {}
    exec(compile(tree, method.__code__.co_filename, "exec"), method.__globals__, namespace)
    case.test_scope = namespace["test_scope"].__get__(case)
