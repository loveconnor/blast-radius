"""Reuse verified parse_and_optimize worker results for unaffected fixture rows."""
import functools
import hashlib
import os
import pickle
import zlib
from pathlib import Path


PLAN = None
_FD = None
INDEX = Path(__file__).with_suffix(".idx")
BINARY = Path(__file__).with_suffix(".bin")


@functools.lru_cache(maxsize=1)
def index():
    try:
        with INDEX.open("rb") as fh:
            return pickle.load(fh)
    except FileNotFoundError:
        return None


def plan(lines, settings=()):
    global PLAN
    PLAN = None
    eligible = ("sqlglot/optimizer/", "sqlglot/generators/", "sqlglot/parsers/", "sqlglot/dialects/", "sqlglot/typing/")
    schema_only = bool(settings) and all(
        owner.startswith("sqlglot.expressions.") and attr in ("arg_types", "required_args")
        for owner, attr in settings)
    if schema_only:
        eligible += ("sqlglot/expressions/",)
    if not lines or any(not (path.startswith(eligible) or path in ("sqlglot/generator.py", "sqlglot/parser.py"))
                        for path in lines):
        return {}
    data = index()
    if not data or not set(settings) <= data["tracked"]:
        return {}
    affected = 0
    for key in settings:
        affected |= data["reads"].get(key, 0)
    for path, numbers in lines.items():
        table = data["lines"].get(path, {})
        for number in numbers:
            affected |= table.get(number, 0)
    PLAN = affected, data["keys"]
    fractions = {}
    for test, (elapsed, costs) in data["costs"].items():
        saved = sum(seconds for i, seconds in costs.items() if not affected & (1 << i))
        if saved and elapsed:
            fractions[test] = max(0.2, 1 - 0.8 * saved / elapsed)
    return fractions


def result(payload):
    global _FD
    affected, keys = PLAN
    record = keys.get(hashlib.sha256(payload).digest())
    if record is None:
        return None
    i, offset, length = record
    if affected & (1 << i):
        return None
    if _FD is None:
        _FD = os.open(BINARY, os.O_RDONLY)
    return zlib.decompress(os.pread(_FD, length, offset))
