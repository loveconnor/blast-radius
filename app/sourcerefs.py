"""Look up exact identifier mentions in the pinned source without rescanning files."""
import ast
import functools
import pickle
import re
import zlib
from pathlib import Path

INDEX = Path(__file__).with_suffix(".idx")
DATA = Path(__file__).with_suffix(".bin")
TOKEN = re.compile(r"[A-Za-z0-9_]+")


def _test_lines(path, text, coverage):
    # A literal or symbol mentioned in one test body cannot be read by all
    # other tests in its file. Include callers of the body as well as its id.
    groups = {}
    by_name = {coverage.tests[t]: t for t in coverage.tests_in_file(path)}
    for cls in ast.parse(text).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
                continue
            name = f"{path}::{cls.name}::{fn.name}"
            if name not in by_name:
                continue
            reached = frozenset(coverage.covering(path, fn.lineno, fn.end_lineno) | {by_name[name]})
            for line in range(fn.body[0].lineno, fn.end_lineno + 1):
                groups[line] = reached
    return groups


def build(texts, coverage):
    source = {}
    tests = {}
    shared = set()
    paths = tuple(sorted(texts))
    for file, path in enumerate(paths):
        text = texts[path]
        if path.startswith("tests/"):
            all_tests = coverage.tests_in_file(path)
            if not all_tests:
                shared.update(TOKEN.findall(text))
                continue
            scoped = _test_lines(path, text, coverage)
            for line, content in enumerate(text.split("\n"), 1):
                for word in set(TOKEN.findall(content)):
                    tests.setdefault(word, set()).update(scoped.get(line, all_tests))
        else:
            for line, content in enumerate(text.split("\n"), 1):
                for word in set(TOKEN.findall(content)):
                    source.setdefault(word, {}).setdefault(file, []).append(line)
    ranges = {}
    with DATA.open("wb") as fh:
        for name in sorted(source.keys() | tests.keys() | shared):
            if name[0].isdigit():
                continue
            blob = zlib.compress(pickle.dumps((source.get(name, {}), None if name in shared else tests.get(name, ())), protocol=5), 1)
            ranges[name] = fh.tell(), len(blob)
            fh.write(blob)
    with INDEX.open("wb") as fh:
        pickle.dump((paths, ranges), fh, protocol=5)


@functools.lru_cache(maxsize=1)
def index():
    if not INDEX.exists() or not DATA.exists():
        return None
    with INDEX.open("rb") as fh:
        return pickle.load(fh)


def find(name):
    data = index()
    if data is None:
        return None
    paths, ranges = data
    if name not in ranges:
        return {}, ()
    offset, size = ranges[name]
    with DATA.open("rb") as fh:
        fh.seek(offset)
        source, tests = pickle.loads(zlib.decompress(fh.read(size)))
    return {paths[f]: set(lines) for f, lines in source.items()}, tests
