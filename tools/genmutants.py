"""Generate synthetic mutant patches resembling the track's commits.

usage: python genmutants.py <repo> <outdir> <count> <seed>
"""
import ast
import difflib
import json
import random
import re
import sys
from pathlib import Path

repo = Path(sys.argv[1]); out = Path(sys.argv[2]); count = int(sys.argv[3]); seed = int(sys.argv[4])
rng = random.Random(seed)
out.mkdir(parents=True, exist_ok=True)

py_files = sorted(p for p in (repo / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts)
weights = [p.stat().st_size for p in py_files]
fixtures = sorted(p for p in (repo / "tests" / "fixtures").rglob("*.sql"))
test_files = sorted((repo / "tests").rglob("test_*.py"))

SWAPS = [
    (r" and ", " or "), (r" or ", " and "), (r" == ", " != "), (r" != ", " == "),
    (r" is not ", " is "), (r" is ", " is not "), (r" is None", " == None"), (r" < ", " <= "),
    (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r"if not ", "if "), (r"if (?!not )", "if not "),
    (r" \+ 1\b", " - 1"), (r" - 1\b", " + 1"), (r"\[0\]", "[1]"), (r"\[1\]", "[0]"),
    (r"\[-1\]", "[0]"), (r", 0\)", ", 1)"), (r", 1\)", ", 2)"), (r"\breturn None\b", "return"),
    (r" in ", " not in "), (r" not in ", " in "), (r"copy=False", "copy=True"),
    (r"copy=True", "copy=False"), (r"\bNone\b", "0"), (r"\b(\d+)\b", None), (r"'([A-Z_]+)'", None),
    (r'"([A-Z_]+)"', None), (r"\bbreak\b", "continue"), (r"\.lower\(\)", ".upper()"),
    (r"\.upper\(\)", ".lower()"), (r"\ball\(", "any("), (r"\bany\(", "all("), (r"\bmin\(", "max("),
    (r"\bmax\(", "min("),
]
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator",
            "# Intentionally not cached", "# Mirrors the behavior of the reference implementation",
            "# TODO: revisit", "# NOTE: order matters here"]


def unified(rel, old, new):
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             f"a/{rel}", f"b/{rel}", n=3)
    body = "".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in d)
    if not body:
        return None
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body


def mutate_code(path):
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    for _ in range(200):
        i = rng.randrange(len(lines))
        line = lines[i]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith('"""') or s.startswith("import") or s.startswith("from "):
            continue
        pat, rep = rng.choice(SWAPS)
        ms = list(re.finditer(pat, line))
        if not ms:
            continue
        m = rng.choice(ms)
        if rep is None:
            if pat.startswith(r"\b(\d"):
                v = int(m.group(1)); r = str(v + rng.choice([1, -1]) if v > 0 else 1)
                new = line[:m.start(1)] + r + line[m.end(1):]
            else:
                g = m.group(1); r = g[:-1] if len(g) > 2 else g + "X"
                new = line[:m.start(1)] + r + line[m.end(1):]
        else:
            new = line[:m.start()] + rep + line[m.end():]
        if new == line:
            continue
        nsrc = "".join(lines[:i] + [new] + lines[i + 1:])
        try:
            ast.parse(nsrc)
        except SyntaxError:
            continue
        return nsrc
    return None


def comment_code(path):
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    for _ in range(100):
        i = rng.randrange(1, len(lines))
        prev = lines[i]
        indent = re.match(r"\s*", prev).group(0)
        if not prev.strip():
            continue
        new = lines[:i] + [f"{indent}{rng.choice(COMMENTS)}\n"] + lines[i:]
        nsrc = "".join(new)
        try:
            ast.parse(nsrc)
        except SyntaxError:
            continue
        return nsrc
    return None


def mutate_fixture(path):
    src = path.read_text()
    lines = src.splitlines(keepends=True)
    for _ in range(100):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.startswith("--"):
            continue
        words = list(re.finditer(r"\S+", line))
        if not words:
            continue
        op = rng.randrange(4)
        w = rng.choice(words)
        if op == 0:
            new = line[:w.end()] + " 42" + line[w.end():]
        elif op == 1:
            new = line[:w.start()] + line[w.end():]
        elif op == 2:
            new = line[:w.start()] + w.group(0)[:-1] + line[w.end():] if len(w.group(0)) > 1 else line + ""
        else:
            new = line[:w.start()] + "NULL " + line[w.start():]
        if new == line:
            continue
        return "".join(lines[:i] + [new] + lines[i + 1:])
    return None


def mutate_test(path):
    return mutate_code(path)


kinds = (["code"] * 55 + ["fixture"] * 22 + ["comment"] * 10 + ["multi"] * 9 + ["test"] * 4)
made = 0
meta = {}
while made < count:
    kind = rng.choice(kinds)
    parts = []
    if kind == "code":
        p = rng.choices(py_files, weights)[0]; n = mutate_code(p); parts = [(p, n)]
    elif kind == "comment":
        p = rng.choices(py_files, weights)[0]; n = comment_code(p); parts = [(p, n)]
    elif kind == "fixture":
        p = rng.choice(fixtures); n = mutate_fixture(p); parts = [(p, n)]
    elif kind == "test":
        p = rng.choice(test_files); n = mutate_test(p); parts = [(p, n)]
    else:
        ps = rng.sample(py_files, 2)
        parts = [(ps[0], comment_code(ps[0]))]
        if rng.random() < 0.5:
            f = rng.choice(fixtures); parts.append((f, mutate_fixture(f)))
        else:
            parts.append((ps[1], mutate_code(ps[1])))
    if any(n is None for _, n in parts):
        continue
    text = ""
    for p, n in sorted(parts, key=lambda x: str(x[0])):
        rel = str(p.relative_to(repo))
        u = unified(rel, p.read_text(), n)
        if u is None:
            text = None
            break
        text += u
    if not text:
        continue
    cid = f"m{seed}_{made:04d}"
    (out / f"{cid}.patch").write_text(text)
    meta[cid] = kind
    made += 1
(out / "kinds.json").write_text(json.dumps(meta))
