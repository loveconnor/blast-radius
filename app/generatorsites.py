"""Recognize keyed SQL calls whose comment flag is unused by the observed receiver."""
import ast
import functools
import gzip
import json
from pathlib import Path

INDEX = Path(__file__).with_name("generatorsites.json.gz")
GEN = "sqlglot/generator.py"


def _keyed_calls(tree):
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "self" and call.func.attr == "sql"
                and len(call.args) == 2 and isinstance(call.args[0], ast.Name)
                and isinstance(call.args[1], ast.Constant) and type(call.args[1].value) is str and call.args[1].value
                and len(call.keywords) == 1 and call.keywords[0].arg == "comment"):
            continue
        comment = call.keywords[0].value
        if isinstance(comment, ast.Constant) and type(comment.value) is bool and call.lineno == call.end_lineno:
            yield comment


@functools.lru_cache(maxsize=1)
def index():
    try:
        with gzip.open(INDEX, "rt") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def cleared(files, repo, coverage):
    data = index()
    if not data or len(files) != 1 or data["tests"] != coverage.tests:
        return set()
    path, new = next(iter(files.items()))
    sites = data["sites"].get(path)
    if not sites or not new:
        return set()
    old = (repo / path).read_bytes().splitlines(keepends=True)
    edited = new.encode().splitlines(keepends=True)
    if len(old) != len(edited):
        return set()
    changed = [i for i, (a, b) in enumerate(zip(old, edited)) if a != b]
    if len(changed) != 1:
        return set()
    i = changed[0]
    for col, end, value, group in sites.get(str(i + 1), ()):
        replacement = b"False" if value else b"True"
        if edited[i] == old[i][:col] + replacement + old[i][end:]:
            return set(data["groups"][group])
    return set()
