"""Identify which truth values can change a side-effect-free conditional."""
import ast
import functools
import gzip
import pickle
from pathlib import Path


def formula(node, atoms):
    def atom(spec):
        if spec not in atoms:
            atoms.append(spec)
        return atoms.index(spec)

    if isinstance(node, ast.Name):
        return atom(("local", node.id))
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return atom(("field", node.attr))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        body = formula(node.operand, atoms)
        return None if body is None else ("not", body)
    if isinstance(node, ast.BoolOp):
        args = tuple(formula(v, atoms) for v in node.values)
        return None if None in args else ("and" if isinstance(node.op, ast.And) else "or", *args)
    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Name) and node.func.id == "isinstance" and len(node.args) == 2
                and not node.keywords and isinstance(node.args[0], ast.Name)):
            klass = node.args[1]
            if isinstance(klass, ast.Attribute) and isinstance(klass.value, ast.Name) and klass.value.id == "exp":
                return atom(("instance", node.args[0].id, klass.attr))
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "_match"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
                and len(node.args) == 1 and len(node.keywords) == 1
                and node.keywords[0].arg == "advance" and isinstance(node.keywords[0].value, ast.Constant)
                and node.keywords[0].value.value is False):
            token = node.args[0]
            if isinstance(token, ast.Attribute) and isinstance(token.value, ast.Name) and token.value.id == "TokenType":
                return atom(("peek", "self", token.attr))
        return None
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.left, ast.Name):
        return None
    op = node.ops[0]
    right = node.comparators[0]
    if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(right, ast.Constant) and right.value is None:
        n = atom(("none", node.left.id))
        return ("not", n) if isinstance(op, ast.IsNot) else n
    if (isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Attribute)
            and isinstance(right.value, ast.Name) and right.value.id == "self" and right.attr.isupper()):
        n = atom(("member", node.left.id, right.attr))
        return ("not", n) if isinstance(op, ast.NotIn) else n
    return None


def evaluate(tree, mask):
    if isinstance(tree, int):
        return bool(mask & (1 << tree))
    if tree[0] == "not":
        return not evaluate(tree[1], mask)
    terms = (evaluate(n, mask) for n in tree[1:])
    return all(terms) if tree[0] == "and" else any(terms)


@functools.lru_cache(maxsize=1)
def index():
    try:
        with gzip.open(Path(__file__).with_name("guards.pkl.gz"), "rb") as fh:
            return pickle.load(fh)
    except (FileNotFoundError, EOFError):
        return None


def unchanged(path, before, edited, ids, row, uncovered=0):
    from comparisonreads import covering, original_tree, restored, stable_or_unseen
    original = original_tree(index, path, before, ids, row)
    if original is None:
        return set()
    data = index()
    kinds = (ast.If, ast.IfExp, ast.While)
    old_tests = {(n.lineno, n.col_offset): n.test for n in covering(original, row)
                 if isinstance(n, kinds) and n.test.lineno <= row <= n.test.end_lineno}
    for n in covering(edited, row):
        if not isinstance(n, kinds) or not n.test.lineno <= row <= n.test.end_lineno:
            continue
        old = old_tests.get((n.lineno, n.col_offset))
        if old is None:
            continue
        site = data["sites"].get((path, old.lineno, old.col_offset))
        if site not in data["reads"]:
            continue
        atoms = []
        left = formula(old, atoms)
        count = len(atoms)
        right = formula(n.test, atoms)
        if left is None or right is None or count != len(atoms) or left == right:
            continue
        saved = n.test
        try:
            n.test = old
            if not restored(original, edited, row):
                continue
        finally:
            n.test = saved
        allowed = stable_or_unseen(data, site, uncovered)
        for mask, readers in data["reads"][site].items():
            if not isinstance(mask, int) or evaluate(left, mask) != evaluate(right, mask):
                allowed &= ~readers
        return {i for i in range(len(ids)) if allowed & (1 << i)}
    return set()
