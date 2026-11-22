"""Look up exact identifier mentions in the pinned source without rescanning files."""
import ast
import dis
import io
import tokenize
import types
import functools
import gzip
import json
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


def _binding_text(path, text):
    """Mask comments and bytecode-proven local variable names, preserving lines."""
    if any(name in text for name in ("getsource(", "getframeinfo(", "f_locals", "locals()", "eval(", "exec(")):
        return text.split("\n")
    lines = [bytearray(line.encode()) for line in text.split("\n")]
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            raw = lines[row - 1]
            del raw[len(raw.decode()[:col].encode()):]
    todo = [compile(text, path, "exec", dont_inherit=True)]
    while todo:
        code = todo.pop()
        todo.extend(item for item in code.co_consts if isinstance(item, types.CodeType))
        for op in dis.get_instructions(code):
            if op.opname not in ("LOAD_FAST", "LOAD_FAST_CHECK", "STORE_FAST", "DELETE_FAST",
                                 "LOAD_DEREF", "STORE_DEREF", "DELETE_DEREF"):
                continue
            pos = op.positions
            if pos.lineno is None or pos.lineno != pos.end_lineno or pos.col_offset is None:
                continue
            raw = lines[pos.lineno - 1]
            word = op.argval.encode()
            if raw[pos.col_offset:pos.end_col_offset] == word:
                raw[pos.col_offset:pos.end_col_offset] = b" " * len(word)
    return [line.decode() for line in lines]


def build(texts, coverage):
    source = {}
    bare = {}
    tests = {}
    shared = set()
    paths = tuple(sorted(texts))
    for file, path in enumerate(paths):
        text = texts[path]
        binding_lines = _binding_text(path, text)
        if path.startswith("tests/"):
            all_tests = coverage.tests_in_file(path)
            if not all_tests:
                shared.update(TOKEN.findall("\n".join(binding_lines)))
                continue
            scoped = _test_lines(path, text, coverage)
            for line, content in enumerate(binding_lines, 1):
                for word in set(TOKEN.findall(content)):
                    tests.setdefault(word, set()).update(scoped.get(line, all_tests))
        else:
            # Record lines where *every* textual occurrence of a name is a
            # LOAD_GLOBAL. Attribute reads, literals and reflective references
            # on the same line keep the ordinary conservative treatment.
            loads = {}
            todo = [compile(text, path, "exec", dont_inherit=True)]
            while todo:
                code = todo.pop()
                todo.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
                for op in dis.get_instructions(code):
                    pos = op.positions
                    if op.opname == "LOAD_GLOBAL" and pos.lineno == pos.end_lineno and pos.lineno:
                        loads.setdefault((pos.lineno, op.argval), set()).add((pos.col_offset, pos.end_col_offset))
            for line, content in enumerate(binding_lines, 1):
                for word in set(TOKEN.findall(content)):
                    source.setdefault(word, {}).setdefault(file, []).append(line)
                    offsets = loads.get((line, word))
                    if not offsets:
                        continue
                    encoded = content.encode()
                    if all(encoded[a:b] == word.encode() for a, b in offsets):
                        scrubbed = bytearray(encoded)
                        for a, b in offsets:
                            scrubbed[a:b] = b" " * (b - a)
                        if word not in TOKEN.findall(scrubbed.decode()):
                            bare.setdefault(word, {}).setdefault(file, []).append(line)
    ranges = {}
    with DATA.open("wb") as fh:
        for name in sorted(source.keys() | tests.keys() | shared):
            if name[0].isdigit():
                continue
            blob = zlib.compress(pickle.dumps((source.get(name, {}), None if name in shared else tests.get(name, ()), bare.get(name, {})), protocol=5), 1)
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


@functools.lru_cache(maxsize=128)
def _lookup(name):
    data = index()
    if data is None:
        return None
    paths, ranges = data
    if name not in ranges:
        return {}, (), {}
    offset, size = ranges[name]
    with DATA.open("rb") as fh:
        fh.seek(offset)
        source, tests, *rest = pickle.loads(zlib.decompress(fh.read(size)))
    def expand(groups):
        return {paths[f]: set(lines) for f, lines in groups.items()}
    return expand(source), tests, expand(rest[0]) if rest else {}


def find(name):
    result = _lookup(name)
    return result[:2] if result is not None else None


def global_loads(name):
    result = _lookup(name)
    return result[2] if result is not None else {}


@functools.lru_cache(maxsize=1)
def qualified_readers():
    """Per-site tests whose receiver was a builtin dialect or a standard library module."""
    path = Path(__file__).with_name("bindings.json.gz")
    if not path.exists():
        return (), {}
    with gzip.open(path, "rt") as fh:
        data = json.load(fh)
    sites = {}
    for location, tests in data["sites"].items():
        file, line, name = location.split("|")
        sites.setdefault(name, {})[(file, int(line))] = frozenset(tests)
    return data["tests"], sites
