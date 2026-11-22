"""Look up exact identifier mentions in the pinned source without rescanning files."""
import functools
import pickle
import re
import zlib
from pathlib import Path

INDEX = Path(__file__).with_suffix(".idx")
DATA = Path(__file__).with_suffix(".bin")
TOKEN = re.compile(r"[A-Za-z0-9_]+")


def build(texts):
    source = {}
    tests = {}
    paths = tuple(sorted(texts))
    for file, path in enumerate(paths):
        text = texts[path]
        if path.startswith("tests/"):
            for word in set(TOKEN.findall(text)):
                tests.setdefault(word, []).append(file)
        else:
            for line, content in enumerate(text.split("\n"), 1):
                for word in set(TOKEN.findall(content)):
                    source.setdefault(word, {}).setdefault(file, []).append(line)
    ranges = {}
    with DATA.open("wb") as fh:
        for name in sorted(source.keys() | tests.keys()):
            if name[0].isdigit():
                continue
            blob = zlib.compress(pickle.dumps((source.get(name, {}), tests.get(name, ())), protocol=5), 1)
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
    return {paths[f]: set(lines) for f, lines in source.items()}, [paths[f] for f in tests]
