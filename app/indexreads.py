"""Track literal indices that selected the same object in baseline sequences."""
import ast
import functools
import gzip
import pickle
from pathlib import Path

INDICES = (-2, -1, 0, 1, 2)


def literal(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
            return -node.operand.value
    return None


@functools.lru_cache(maxsize=1)
def index():
    try:
        with gzip.open(Path(__file__).with_name("indices.pkl.gz"), "rb") as fh:
            return pickle.load(fh)
    except (FileNotFoundError, EOFError):
        return None


def unchanged(path, before, edited, ids, row):
    from comparisonreads import covering, original_tree, restored
    original = original_tree(index, path, before, ids, row)
    if original is None:
        return set()
    data = index()
    old_nodes = {(n.lineno, n.col_offset): n for n in covering(original, row)
                 if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)}
    for node in covering(edited, row):
        if not isinstance(node, ast.Subscript) or not isinstance(node.ctx, ast.Load):
            continue
        old = old_nodes.get((node.lineno, node.col_offset))
        if old is None:
            continue
        site = data["sites"].get((path, old.lineno, old.col_offset))
        old_index, new_index = literal(old.slice), literal(node.slice)
        if site not in data["reads"] or new_index not in INDICES or old_index == new_index:
            continue
        saved = node.slice
        try:
            node.slice = old.slice
            if not restored(original, edited, row):
                continue
        finally:
            node.slice = saved
        allowed = data["both"].get(site, 0) & ~data["unknown"]
        bit = 1 << INDICES.index(new_index)
        for mask, readers in data["reads"][site].items():
            if not isinstance(mask, int) or not mask & bit:
                allowed &= ~readers
        return {i for i in range(len(ids)) if allowed & (1 << i)}
    return set()
