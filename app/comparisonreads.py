"""Per-test values at comparisons against literal constants in the pinned suite."""
import ast
import functools
import gzip
import hashlib
import operator
import pickle
from pathlib import Path

OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Is: operator.is_,
       ast.IsNot: operator.is_not, ast.Lt: operator.lt, ast.LtE: operator.le,
       ast.Gt: operator.gt, ast.GtE: operator.ge}


@functools.lru_cache(maxsize=1)
def index():
    try:
        with gzip.open(Path(__file__).with_name("comparisons.pkl.gz"), "rb") as fh:
            return pickle.load(fh)
    except (FileNotFoundError, EOFError):
        return None


def operation(node):
    if len(node.ops) != 1 or type(node.ops[0]) not in OPS or not isinstance(node.comparators[0], ast.Constant):
        return None
    op = type(node.ops[0])
    value = node.comparators[0].value
    if type(value) not in (type(None), str, bool, int):
        return None
    return OPS[op], value


def agrees(sample, old, new):
    if sample[0] == "unknown":
        return False
    if sample[0] == "container":
        # The tracked containers cannot equal any of these scalar literals.
        outcomes = {operator.eq: False, operator.is_: False, operator.ne: True, operator.is_not: True}
        return old[0] in outcomes and new[0] in outcomes and outcomes[old[0]] == outcomes[new[0]]
    # Distinct primitive values cannot be identical. Equal strings and
    # integers may be interned, so a stored value cannot establish identity.
    for func, literal in (old, new):
        if (func in (operator.is_, operator.is_not) and type(literal) in (int, str)
                and type(sample[1]) is type(literal) and sample[1] == literal):
            return False
    try:
        return old[0](sample[1], old[1]) == new[0](sample[1], new[1])
    except TypeError:
        # A change in exception text or type can be observable.
        return False


def covering(tree, row):
    """Visit AST nodes whose source spans the single edited line."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if hasattr(node, "lineno") and not node.lineno <= row <= node.end_lineno:
            # Decorators precede the def/class line.
            stack.extend(getattr(node, "decorator_list", ()))
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def restored(a, b, row):
    """Compare the complete restored tree, skipping verbatim sibling statements."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(restored(x, y, row) for x, y in zip(a, b))
    if not isinstance(a, ast.AST):
        return a == b
    if isinstance(a, ast.stmt):
        start = min([a.lineno] + [d.lineno for d in getattr(a, "decorator_list", ())])
        if (row < start or row > a.end_lineno) and (
            a.lineno, a.end_lineno, a.col_offset, a.end_col_offset
        ) == (b.lineno, b.end_lineno, b.col_offset, b.end_col_offset):
            # A decorated definition could have acquired a decorator from the
            # edited line. Compare those before skipping the identical body.
            return restored(getattr(a, "decorator_list", []), getattr(b, "decorator_list", []), row)
    return all(restored(getattr(a, field, None), getattr(b, field, None), row) for field in a._fields)


@functools.lru_cache(maxsize=128)
def deterministic_function(tree, line):
    enclosing = [n for n in covering(tree, line) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.lineno <= line <= n.end_lineno]
    if not enclosing:
        return False
    fn = min(enclosing, key=lambda n: n.end_lineno - n.lineno)
    if fn.name in {"__eq__", "__ne__", "__lt__", "__gt__", "__le__", "__ge__", "__bool__", "__hash__"}:
        return False
    volatile = {"id", "hash", "time", "monotonic", "perf_counter", "getpid", "platform",
                "environ", "getenv", "random", "randint", "getrandbits", "uuid4", "getsizeof"}
    return not any((isinstance(n, ast.Name) and n.id in volatile)
                   or (isinstance(n, ast.Attribute) and n.attr in volatile) for n in ast.walk(fn))


def changed_row(before, after):
    a, b = before.splitlines(), after.splitlines()
    if len(a) != len(b):
        return None
    rows = [i for i, (x, y) in enumerate(zip(a, b), 1) if x != y]
    return rows[0] if len(rows) == 1 else None


@functools.lru_cache(maxsize=3)
def site_lines(loader):
    data = loader()
    return {(p, line) for p, line, _ in data["sites"]} if data else set()


def original_tree(loader, path, before, ids, row):
    if (path, row) not in site_lines(loader):
        return None
    data = loader()
    if ids != data["ids"] or hashlib.sha256(before.encode()).digest() != data["files"].get(path):
        return None
    from patchlib import _parse_old
    tree = _parse_old(before)
    return tree if deterministic_function(tree, row) else None


def unchanged(path, before, edited, ids, row):
    original = original_tree(index, path, before, ids, row)
    if original is None:
        return set()
    data = index()
    old_nodes = {(n.lineno, n.col_offset): n for n in covering(original, row)
                 if isinstance(n, ast.Compare) and n.lineno <= row <= n.end_lineno}
    for new in covering(edited, row):
        if not isinstance(new, ast.Compare) or not new.lineno <= row <= new.end_lineno:
            continue
        old = old_nodes.get((new.lineno, new.col_offset))
        if old is None or ast.dump(old.left) != ast.dump(new.left):
            continue
        left, right = operation(old), operation(new)
        site = data["sites"].get((path, old.lineno, old.col_offset))
        if left is None or right is None or site not in data["reads"] or ast.dump(old) == ast.dump(new):
            continue
        ops, comparators = new.ops, new.comparators
        try:
            new.ops, new.comparators = old.ops, old.comparators
            if not restored(original, edited, row):
                continue
        finally:
            new.ops, new.comparators = ops, comparators
        allowed = data["both"].get(site, 0)
        for sample, tests in data["reads"][site].items():
            if not agrees(sample, left, right):
                allowed &= ~tests
        allowed &= ~data["unknown"]
        return {i for i in range(len(ids)) if allowed & (1 << i)}
    return set()
