"""Reach of individual short-circuit operands in the pinned test suite."""
import ast
import functools
import gzip
import hashlib
import pickle
from pathlib import Path


def parts(node):
    if isinstance(node, ast.BoolOp):
        return [(i, arg) for i, arg in enumerate(node.values) if i]
    if isinstance(node, ast.IfExp):
        return [("yes", node.body), ("no", node.orelse)]
    return ()


def replace(node, key, value):
    if isinstance(key, int):
        node.values[key] = value
    elif key == "yes":
        node.body = value
    else:
        node.orelse = value


@functools.lru_cache(maxsize=1)
def index():
    try:
        with gzip.open(Path(__file__).with_name("branchreach.pkl.gz"), "rb") as fh:
            return pickle.load(fh)
    except (FileNotFoundError, EOFError):
        return None


def unchanged(path, before, edited, ids, row):
    data = index()
    if not data or ids != data["ids"] or (path, row) not in data["lines"]:
        return set()
    if hashlib.sha256(before.encode()).digest() != data["files"].get(path):
        return set()
    from comparisonreads import covering, deterministic_function, restored
    from patchlib import _parse_old
    tree = _parse_old(before)
    if not deterministic_function(tree, row):
        return set()
    nodes = {(n.lineno, n.col_offset, type(n)): n for n in covering(tree, row)
             if isinstance(n, (ast.BoolOp, ast.IfExp)) and n.lineno <= row <= n.end_lineno}
    cleared = 0
    for node in covering(edited, row):
        if not isinstance(node, (ast.BoolOp, ast.IfExp)) or not node.lineno <= row <= node.end_lineno:
            continue
        old = nodes.get((node.lineno, node.col_offset, type(node)))
        if old is None:
            continue
        base = path, old.lineno, old.col_offset, type(old).__name__
        if (*base, "parent") not in data["sites"]:
            continue
        old_parts = dict(parts(old))
        for k, target in parts(node):
            previous = old_parts.get(k)
            if previous is None or not target.lineno <= row <= target.end_lineno:
                continue
            # Even an unexecuted yield turns a normal function into a generator.
            # Assignment expressions and nested scopes can change compiler bindings.
            binding_nodes = (ast.Yield, ast.YieldFrom, ast.Await, ast.NamedExpr, ast.Lambda,
                             ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            if any(isinstance(n, binding_nodes) for expr in (previous, target) for n in ast.walk(expr)):
                continue
            if ast.dump(previous) == ast.dump(target):
                continue
            # Replacing just this operand must completely restore the module.
            # This rules out edits that would change whether it gets evaluated.
            replace(node, k, previous)
            try:
                identical = restored(tree, edited, row)
            finally:
                replace(node, k, target)
            if identical:
                parent = data["sites"][(*base, "parent")]
                child = data["sites"][(*base, k)]
                cleared |= data["both"].get(parent, 0) & ~data["either"].get(child, 0)
    cleared &= ~data["unknown"]
    return {i for i in range(len(ids)) if cleared & (1 << i)}
