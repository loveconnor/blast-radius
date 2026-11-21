"""In-process test runner with fork-per-commit and code hot patching.

The zygote imports sqlglot and the needed test modules once. For every commit it
forks a child that applies the commit in memory (swapping function code objects
when the change is confined to function bodies, otherwise re-importing sqlglot
from an overlay directory), runs the requested tests with unittest and reports
which of them failed.
"""
from __future__ import annotations

import builtins
import gc
import importlib
import io
import json
import os
import pickle
import signal
import sys
import time
import types
import unittest

REPO = os.environ.get("BR_REPO", "/opt/sqlglot")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------- inline pools
import concurrent.futures as _cf


class InlineExecutor:
    """Runs submitted work synchronously, with a pickle round trip like a real pool."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        n = _SUB["n"]
        _SUB["n"] = n + 1
        allow = _SUB["allow"]
        if allow is not None and n not in allow:
            f = _cf.Future()
            f.set_result(None)  # query not reaching the changed code: skip its comparison
            return f
        if not _SUB["lazy"]:
            f = _cf.Future()
            try:
                fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
                f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:  # noqa
                f.set_exception(e)
            return f
        f = _LazyFuture()
        try:
            f._job = pickle.loads(pickle.dumps((fn, a, k)))
        except BaseException as e:  # noqa
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [self.submit(fn, *args).result() for args in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


_SUB = {"n": 0, "allow": None, "failed": False, "lazy": False}
SUBMIT_LAZY: set = set()  # tests that only read futures one by one via .result()


class _LazyFuture(_cf.Future):
    """Runs the submitted call when its result is first asked for. Once the
    running test has a failing subtest, later calls are skipped (the test fails anyway)."""
    _job = None

    def _run(self):
        job, self._job = self._job, None
        if job is None:
            return
        if _SUB["failed"]:
            self.set_result(None)
            return
        fn, a, k = job
        try:
            self.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa
            self.set_exception(e)

    def result(self, timeout=None):
        self._run()
        return super().result(timeout)

    def exception(self, timeout=None):
        self._run()
        return super().exception(timeout)
SUBMIT_FILTER: dict = {}  # test number -> set of submit indices to really execute


def install_inline_pools():
    _cf.ProcessPoolExecutor = InlineExecutor
    import concurrent.futures.process as _p
    _p.ProcessPoolExecutor = InlineExecutor


# --------------------------------------------------------------- file overlay
_orig_open = builtins.open
_redirect: dict[str, str] = {}


def _open(file, *a, **k):
    if _redirect:
        try:
            p = os.fspath(file)
            if isinstance(p, str):
                r = _redirect.get(os.path.realpath(p))
                if r is not None:
                    file = r
        except TypeError:
            pass
    return _orig_open(file, *a, **k)


def install_open_hook():
    builtins.open = _open
    io.open = _open


# --------------------------------------------------------------- zygote setup
def module_name_for(rel):
    """tests/dialects/test_x.py -> tests.dialects.test_x"""
    return rel[:-3].replace("/", ".")


def zygote_import(test_modules):
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, REPO)
    # subprocesses started by tests (e.g. test_lazy_load) must find sqlglot too
    os.environ["PYTHONPATH"] = REPO + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    install_inline_pools()
    install_open_hook()
    import sqlglot  # noqa
    if os.environ.get("BR_WARM", "1") == "1":
        import pkgutil
        for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if m.name.endswith("__main__"):
                continue
            try:
                importlib.import_module(m.name)
            except Exception:  # noqa
                pass
    for m in sorted(test_modules):
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa
            log("zygote import failed", m, repr(e))
    if os.environ.get("BR_WARM", "1") == "1":
        build_func_map()


# --------------------------------------------------------------- hot patching
def _walk_codes(code, out, prefix=()):
    out.append(code)
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _walk_codes(c, out)
    return out


class NeedReimport(Exception):
    pass


DIFF_HITS: dict = {}
DIFF_BAD: set = set()


def _br_d(k, fo, fn, b):
    v = fn()
    DIFF_HITS[k] = DIFF_HITS.get(k, 0) + 1
    if k in DIFF_BAD:
        return v
    try:
        o = fo()
    except BaseException:  # noqa
        DIFF_BAD.add(k)
        return v
    try:
        if b:
            same = bool(o) == bool(v)
        else:
            same = o is v or (type(o) is type(v) and bool(o == v))
    except BaseException:  # noqa
        same = False
    if not same:
        DIFF_BAD.add(k)
    return v


builtins._br_diff_ = _br_d


def hot_patch(rel, old_src, new_src, instr_tree=None):
    """Swap code objects of changed functions. With instr_tree, the swapped code is the
    instrumented version (old and new expression evaluated side by side).
    Returns (swapped, instrumented: bool)."""
    path = os.path.join(REPO, rel)
    old_codes = _walk_codes(compile(old_src, path, "exec", dont_inherit=True), [])
    new_codes = _walk_codes(compile(new_src, path, "exec", dont_inherit=True), [])
    if len(old_codes) != len(new_codes):
        raise NeedReimport("code count differs")
    pairs = {}
    for o, n in zip(old_codes, new_codes):
        if o.co_qualname != n.co_qualname or o.co_freevars != n.co_freevars:
            raise NeedReimport("code shape differs")
        if o is old_codes[0]:
            continue  # module body
        if o != n:
            pairs[o] = n
    if not pairs:
        return 0, False
    instrumented = False
    if instr_tree is not None:
        try:
            icodes = _walk_codes(compile(instr_tree, path, "exec", dont_inherit=True), [])
            bykey = {}
            for c in icodes:
                bykey.setdefault((c.co_qualname, c.co_firstlineno), []).append(c)
            ipairs = {}
            for o, n in pairs.items():
                cand = bykey.get((n.co_qualname, n.co_firstlineno), [])
                cand = [c for c in cand if c.co_freevars == n.co_freevars and c.co_argcount == n.co_argcount]
                if len(cand) != 1:
                    raise NeedReimport("instr mismatch")
                ipairs[o] = cand[0]
            pairs = ipairs
            instrumented = True
        except NeedReimport:
            instrumented = False
        except Exception:  # noqa
            instrumented = False
    swapped = 0
    funcs = FUNC_MAP.get(path) if FUNC_MAP is not None else None
    if funcs is None:
        funcs = [o for o in gc.get_objects() if type(o) is types.FunctionType and o.__code__.co_filename == path]
    for obj in funcs:
        n = pairs.get(obj.__code__)
        if n is not None:
            obj.__code__ = n
            swapped += 1
    return swapped, instrumented


FUNC_MAP = None


def build_func_map():
    """filename -> live function objects, built once in the zygote."""
    global FUNC_MAP
    m = {}
    for o in gc.get_objects():
        if type(o) is types.FunctionType:
            fn = o.__code__.co_filename
            if fn.startswith(REPO):
                m.setdefault(fn, []).append(o)
    FUNC_MAP = m


# --------------------------------------------------------------- overlay
def build_overlay(root, files):
    """Mirror REPO's sqlglot/ and tests/ under root with symlinks; write changed files."""
    for top in ("sqlglot", "tests"):
        src_top = os.path.join(REPO, top)
        for dirpath, dirnames, filenames in os.walk(src_top):
            rel = os.path.relpath(dirpath, REPO)
            dst = os.path.join(root, rel)
            os.makedirs(dst, exist_ok=True)
            for d in list(dirnames):
                if d == "__pycache__" or d == "fixtures":
                    os.symlink(os.path.join(dirpath, d), os.path.join(dst, d))
                    dirnames.remove(d)
            for f in filenames:
                os.symlink(os.path.join(dirpath, f), os.path.join(dst, f))
    for rel, content in files.items():
        dst = os.path.join(root, rel)
        if rel.startswith("tests/fixtures/"):
            continue  # served by the open() hook
        if os.path.lexists(dst):
            os.remove(dst)
        if content is not None:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with _orig_open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)


def reimport(root, test_modules):
    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
            del sys.modules[name]
    sys.path.insert(0, root)
    os.environ["PYTHONPATH"] = root + os.pathsep + os.environ.get("PYTHONPATH", "")
    importlib.invalidate_caches()
    import sqlglot  # noqa


# --------------------------------------------------------------- running
class Result(unittest.TestResult):
    def __init__(self, on_done=None):
        super().__init__()
        self.bad = set()
        self.done = set()
        self.on_done = on_done

    def stopTest(self, test):
        super().stopTest(test)
        i = self._id(test)
        if i is not None:
            self.done.add(i)
        if self.on_done is not None and self.on_done(i, self.bad):
            self.stop()

    def _id(self, test):
        return getattr(test, "_br_id", None)

    def addError(self, test, err):
        i = self._id(test)
        if i is None:
            # class/module fixture failure (_ErrorHolder): mark whole class
            self.bad.add(("CLASS", str(test)))
        else:
            self.bad.add(i)

    def addFailure(self, test, err):
        self.bad.add(self._id(test))

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad.add(self._id(test))
            _SUB["failed"] = True

    def addUnexpectedSuccess(self, test):
        self.bad.add(self._id(test))


class Timeout(BaseException):
    pass


def _alarm(signum, frame):
    raise Timeout()


def _timed(run, seconds, t=None):
    def wrapped(*a, **k):
        _SUB["n"] = 0
        _SUB["failed"] = False
        _SUB["lazy"] = t in SUBMIT_LAZY
        _SUB["allow"] = SUBMIT_FILTER.get(t)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return run(*a, **k)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return wrapped


def run_tests(test_ids, nodeids, timeouts=None, should_stop=None, skipped=None):
    """test_ids: list of numbers; nodeids: number -> 'tests/x.py::Class::method'.
    Returns set of failing numbers. should_stop(failed_so_far, remaining) -> bool stops
    early; tests not run are added to `skipped`."""
    failed = set()
    remaining = list(test_ids)
    stopped = [False]
    by_class = {}
    tmo = {}
    for k, t in enumerate(test_ids):
        tmo[t] = timeouts[k] if timeouts else 60.0
        parts = nodeids[t].split("::")
        mod = module_name_for(parts[0])
        if len(parts) == 3:
            by_class.setdefault((mod, parts[1]), []).append((t, parts[2]))
        else:
            failed.add(t)  # unknown layout, treat as failing to be safe
    signal.signal(signal.SIGALRM, _alarm)
    order_classes = list(by_class.items())
    for ci, ((mod, cls), items) in enumerate(order_classes):
        if stopped[0]:
            if skipped is not None:
                skipped.extend(t for t, _ in items)
            continue
        try:
            m = sys.modules.get(mod) or importlib.import_module(mod)
            C = getattr(m, cls)
        except BaseException:  # noqa
            failed.update(t for t, _ in items)
            continue
        suite = unittest.TestSuite()
        for t, meth in items:
            try:
                case = C(meth)
            except BaseException:  # noqa
                failed.add(t)
                continue
            case._br_id = t
            case.run = _timed(case.run, tmo[t], t)
            suite.addTest(case)
        def on_done(i, bad, _items=items, _ci=ci):
            if should_stop is None:
                return False
            cur = set(failed)
            for b in bad:
                if not isinstance(b, tuple) and b is not None:
                    cur.add(b)
            if not cur:
                return False
            done_here = res.done
            rest = [t for t, _ in _items if t not in done_here] + [t for _, its in order_classes[_ci + 1:] for t, _ in its]
            if rest and should_stop(cur, rest):
                stopped[0] = True
                return True
            return False

        res = Result(on_done)
        try:
            suite.run(res)
        except BaseException:  # noqa
            failed.update(t for t, _ in items)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        for b in res.bad:
            if isinstance(b, tuple) or b is None:
                failed.update(t for t, _ in items)
            else:
                failed.add(b)
        if stopped[0] and skipped is not None:
            skipped.extend(t for t, _ in items if t not in res.done and t not in failed)
    return failed


def prepare(job, modules):
    """Apply a commit inside this (child) process."""
    files = job["files"]
    tmp = job["tmp"]
    for rel, content in files.items():
        if rel.endswith(".py"):
            continue
        dst = os.path.join(tmp, "fx", rel.replace("/", "__"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if content is None:
            _redirect[os.path.realpath(os.path.join(REPO, rel))] = os.path.join(tmp, "missing", "nofile")
        else:
            with _orig_open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
            _redirect[os.path.realpath(os.path.join(REPO, rel))] = dst
    if job["overlay"]:
        build_overlay(os.path.join(tmp, "ov"), {k: v for k, v in files.items() if k.endswith(".py")})
        reimport(os.path.join(tmp, "ov"), modules)
    else:
        all_instr = True
        for rel in job["hot"]:
            tree = job.get("instr", {}).get(rel)
            _, ok = hot_patch(rel, job["olds"][rel], files[rel], tree)
            all_instr = all_instr and ok and tree is not None
        return all_instr and bool(job.get("instr"))
    return False
