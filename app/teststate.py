"""Find class settings that a test can leave behind when its cleanup loses a symbol."""
import ast
import importlib


def _types(module, function, name):
    imports = [stmt for stmt in module.body if isinstance(stmt, ast.ImportFrom)]
    imports.extend(stmt for stmt in ast.walk(function) if isinstance(stmt, ast.ImportFrom))
    owners = set()
    for stmt in imports:
        if not stmt.module or not stmt.module.startswith("sqlglot") or stmt.level:
            continue
        for alias in stmt.names:
            if (alias.asname or alias.name) != name:
                continue
            try:
                value = getattr(importlib.import_module(stmt.module), alias.name)
            except (AttributeError, ImportError):
                continue
            if isinstance(value, type):
                owners.add(value.__module__ + ":" + value.__qualname__)
    return owners


def cleanup_fields(test_ids, nodeids, root, owner, member):
    fields = set()
    parsed = {}
    for t in test_ids:
        parts = nodeids[t].split("::")
        if len(parts) != 3:
            continue
        path, cls, method = parts
        if path not in parsed:
            text = (root / path).read_text()
            parsed[path] = ast.parse(text) if "finally:" in text and member in text else None
        module = parsed[path]
        if module is None:
            continue
        for group in module.body:
            if not isinstance(group, ast.ClassDef) or group.name != cls:
                continue
            for fn in group.body:
                if not isinstance(fn, ast.FunctionDef) or fn.name != method:
                    continue
                for block in ast.walk(fn):
                    if not isinstance(block, ast.Try) or not block.finalbody:
                        continue
                    cleanup = [n for stmt in block.finalbody for n in ast.walk(stmt)]
                    if not any(isinstance(n, ast.Attribute) and n.attr == member and isinstance(n.value, ast.Name)
                               and owner in _types(module, fn, n.value.id) for n in cleanup):
                        continue
                    for n in ast.walk(block):
                        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name):
                            fields.update((t, c, n.attr) for c in _types(module, fn, n.value.id))
    return fields
