"""Index pure column equalities while preserving SQLGlot's nested-loop result order."""
import ast
from collections import defaultdict
from functools import lru_cache


@lru_cache(maxsize=512)
def equalities(source):
    def column(node):
        match node:
            case ast.Subscript(ast.Subscript(ast.Name("scope"), ast.Constant(str(table))), ast.Constant(str(name))):
                return table, name
        raise ValueError

    def visit(node):
        match node:
            case ast.Constant(True):
                return []
            case ast.Call(ast.Name("EQ"), [left, right], []):
                return [(column(left), column(right))]
            case ast.Call(ast.Name("AND"), [ast.Lambda(a, left), ast.Lambda(b, right)], []):
                if any(args.posonlyargs or args.args or args.vararg or args.kwonlyargs or args.kwarg for args in (a, b)):
                    raise ValueError
                return visit(left) + visit(right)
        raise ValueError

    try:
        return tuple(visit(ast.parse(source, mode="eval").body))
    except (ValueError, SyntaxError, RecursionError):
        return ()


def keys(rows, indices):
    result = []
    last = max(indices)
    for row in rows:
        if type(row) is not tuple or len(row) <= last:
            return None
        key = tuple(row[i] for i in indices)
        # These builtins implement side-effect-free equality and hashing.
        if any(type(x) not in (int, str, bool, float, bytes, type(None)) for x in key):
            return None
        result.append(key)
    return result


def indexed(original):
    from sqlglot.executor.table import Table

    def join(self, spec, left, right, condition, context):
        def ordinary():
            return original(self, spec, left, right, condition, context)

        if condition is None or context is None:
            return ordinary()
        source, target = left.table.rows, right.table.rows
        if len(source) * len(target) < 2048:
            return ordinary()
        width = len(left.columns)
        left_cols, right_cols = [], []
        for first, second in equalities(condition.co_filename):
            positions = []
            for table, name in (first, second):
                if table not in context.tables or name not in context.tables[table].reader.columns:
                    return ordinary()
                positions.append(context.tables[table].reader.columns[name])
            a, b = sorted(positions)
            if not a < width <= b:
                return ordinary()
            left_cols.append(a)
            right_cols.append(b - width)
        if not left_cols:
            return ordinary()
        source_keys, target_keys = keys(source, left_cols), keys(target, right_cols)
        if source_keys is None or target_keys is None:
            return ordinary()
        lookup = defaultdict(list)
        for i, key in enumerate(target_keys):
            if None not in key:
                lookup[key].append(i)
        result = Table(left.columns + right.columns)
        matched_left, matched_right = set(), set()
        for i, key in enumerate(source_keys):
            for j in lookup.get(key, ()):
                row = source[i] + target[j]
                if self._join_matches(row, condition, context):
                    result.append(row)
                    matched_left.add(i)
                    matched_right.add(j)
        context.set_row(source[-1] + target[-1])
        self._append_unmatched_join_rows(result, spec, source, target, matched_left, matched_right)
        return result
    return join
