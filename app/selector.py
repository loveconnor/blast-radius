"""Blast Radius selector.

1. Static analysis of every patch against a per-test coverage map of sqlglot at
   the pinned commit gives the candidate tests that could possibly fail.
2. A zygote process imports sqlglot and the candidate test modules once; for
   every commit a forked child applies the change in memory and runs the
   candidates in-process to find out which of them really fail.
3. Only failing tests (plus any candidate we could not afford to run) are
   printed, packed onto workers to minimise makespan + 2 s per worker.

Anything that goes wrong for a commit falls back to the static candidates.
"""
from __future__ import annotations

import json
import os
import resource
import select
import signal
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

T_START = time.time()
DATA = Path(os.environ.get("BR_DATA", "/data"))
REPO = Path(os.environ.get("BR_REPO", "/opt/sqlglot"))
os.environ["BR_REPO"] = str(REPO)
MAP = Path(os.environ.get("BR_MAP", HERE / "map.json.gz"))

# keep the real stdout for answers only; everything else goes to stderr
_OUT_FD = os.dup(1)
os.dup2(2, 1)
sys.stdout = sys.stderr

from sched import best_schedule  # noqa: E402
from selcore import Map, analyze  # noqa: E402

HEAVY_SECONDS = float(os.environ.get("BR_HEAVY", "1.5"))
SECONDS_PER_COMMIT = float(os.environ.get("BR_WALL_PER_COMMIT", "4.6"))
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.0"))  # our cpu / recorded seconds


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def emit(lines):
    data = "".join(json.dumps(l, separators=(",", ":")) + "\n" for l in lines).encode()
    while data:
        n = os.write(_OUT_FD, data)
        data = data[n:]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    nodeids = [t["id"] for t in tests]
    ms = [round(t["seconds"] * 1000) for t in tests]
    patches = sorted((DATA / "commits").glob("*.patch"))
    n_commits = len(patches)
    deadline = T_START + max(25.0, SECONDS_PER_COMMIT * n_commits)
    global MAP_OBJ
    M = Map(MAP)
    MAP_OBJ = M
    if M.tests != nodeids:
        log("WARNING: map test list differs from tests.json; remapping")
        idx = {t: i for i, t in enumerate(nodeids)}
        remap = [idx.get(t) for t in M.tests]
        M.sets = [frozenset(remap[x] for x in s if remap[x] is not None) for s in M.sets]
        M.opened = {p: {remap[x] for x in s if remap[x] is not None} for p, s in M.opened.items()}
        M.tests = nodeids
        M.n = len(nodeids)
        M.file_of = [t.split("::", 1)[0] for t in nodeids]
        M._file_users = {}
    all_tests = set(range(len(tests)))

    answers = {}
    jobs = []
    for pf in patches:
        cid = pf.stem
        try:
            D = analyze(pf.read_text(encoding="utf-8", errors="replace"), REPO, M)
        except Exception as e:  # noqa
            log(cid, "analyze failed", repr(e))
            answers[cid] = sorted(all_tests)
            continue
        if D.all:
            answers[cid] = sorted(all_tests)
            log(cid, "ALL", " ".join(D.reasons)[:200])
            continue
        cands = sorted(t for t in D.cands if t is not None)
        answers[cid] = cands  # static fallback
        hot = [r for r, c in D.files.items() if r.endswith(".py") and c is not None and (REPO / r).exists()]
        olds = {r: (REPO / r).read_text(encoding="utf-8") for r in hot}
        if not cands:
            log(cid, "none", " ".join(D.reasons)[:200])
            continue
        instr = {}
        n_sites = 0
        if not D.import_change and len(hot) == 1 and all(r.endswith(".py") for r in D.files):
            from patchlib import instrument
            for r in hot:
                try:
                    res = instrument(olds[r], D.files[r])
                except Exception:  # noqa
                    res = None
                if res is None:
                    instr = {}
                    break
                instr[r] = res[0]
                n_sites = res[1]
        subf = {}
        if not D.import_change and all(r.split(" ", 1)[0] in ("rt", "ast-same", "annotation") for r in D.reasons):
            subf = submit_filter(D, cands)
        CPU_OVR.clear()
        CPU_OVR.update(sub_cpu(subf))
        if D.import_change:
            # decided in the child once the import-time state diff is known
            if len(cands) > BROAD_CANDS:
                answers[cid] = sorted(all_tests)  # fallback if the child cannot finish
            light, heavy = [], []
        else:
            light, heavy, est_run, static_cost = plan_split(cands, ms, CPU, False, bool(instr))
        if not light and not D.import_change:
            log(cid, f"static cost={static_cost/1000:.2f}", " ".join(D.reasons)[:150])
            if not D.import_change:
                continue
        jobs.append(dict(
            cid=cid, files=D.files, olds=olds, hot=hot, overlay=bool(D.import_change),
            light=light, heavy=heavy, instr=instr, n_sites=n_sites, subf=subf, cpu_ovr=dict(CPU_OVR),
            precise=sorted(D.precise), broad=sorted(D.cands),
            reasons=" ".join(D.reasons)[:200],
        ))
    CPU_OVR.clear()
    log(f"analysis done in {time.time() - T_START:.2f}s; {len(jobs)} commits to run")

    if jobs:
        run_jobs(jobs, answers, nodeids, ms, deadline)

    lines = []
    for pf in patches:
        cid = pf.stem
        sel = answers.get(cid, sorted(all_tests))
        workers, cost = best_schedule(sel, ms)
        lines.append({"commit": cid, "workers": workers})
    emit(lines)
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rc = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"done in {time.time() - T_START:.1f}s cpu self={ru.ru_utime + ru.ru_stime:.1f} children={rc.ru_utime + rc.ru_stime:.1f}")


CPU_PATH = Path(os.environ.get("BR_CPU", HERE / "cpu.json"))
try:
    _cpu = json.loads(CPU_PATH.read_text())
except Exception:  # noqa
    _cpu = {"test": {}, "class": {}}
CPU = _cpu
OVERHEAD_HOT = float(os.environ.get("BR_OVH_HOT", "0.25"))
OVERHEAD_OVERLAY = float(os.environ.get("BR_OVH_OV", "2.0"))
FAIL_PENALTY = float(os.environ.get("BR_FAIL_PEN", "1.5"))


CPU_OVR = {}
SUBMAP_PATH = Path(os.environ.get("BR_SUBMAP", HERE / "submap.json.gz"))
SUB_BASE = float(os.environ.get("BR_SUB_BASE", "0.15"))
_SUBMAP = None


def _submap():
    global _SUBMAP
    if _SUBMAP is None:
        import gzip
        try:
            with gzip.open(SUBMAP_PATH, "rt") as fh:
                raw = json.load(fh)
            _SUBMAP = {int(t): (v["n"], {f: set(ls) for f, ls in v["main"].items()},
                                {f: {int(l): s for l, s in d.items()} for f, d in v["subs"].items()})
                       for t, v in raw.items()}
        except Exception:  # noqa
            _SUBMAP = {}
    return _SUBMAP


def submit_filter(D, cands):
    """For pool-based tests, which submitted queries can reach the changed lines."""
    out = {}
    if not D.rt_lines:
        return out
    for t, (n, main, subs) in _submap().items():
        if t not in cands:
            continue
        allow = set()
        ok = True
        for f, lines in D.rt_lines.items():
            if lines & main.get(f, set()):
                ok = False
                break
            sf = subs.get(f, {})
            for l in lines:
                allow.update(sf.get(l, ()))
        if ok and len(allow) < n:
            out[t] = allow
    return out


def sub_cpu(subf):
    o = {}
    for t, allow in subf.items():
        n = _submap()[t][0]
        v = CPU["test"].get(str(t), 0.0)
        o[t] = v * (SUB_BASE + (1 - SUB_BASE) * len(allow) / max(1, n))
    return o


def cpu_of(t, ms):
    v = CPU_OVR.get(t)
    if v is not None:
        return v * CPU_FACTOR
    v = CPU["test"].get(str(t))
    if v is None:
        v = ms[t] / 1000.0 * 1.2
    return v * CPU_FACTOR


OV_MULT = float(os.environ.get("BR_OV_MULT", "1.6"))
MAX_CHILD_CPU = float(os.environ.get("BR_MAX_CHILD", "20"))
BROAD_CANDS = int(os.environ.get("BR_BROAD_CANDS", "400"))
BROAD_FAILS = int(os.environ.get("BR_BROAD", "3"))
HEAVY_CPU = float(os.environ.get("BR_HEAVY_CPU", "1.0"))
P_EQUIV = float(os.environ.get("BR_P_EQUIV", "0.35"))


def plan_split(cands, ms, cpu, overlay, can_diff=False, overhead=None, mult=1.0):
    """Choose which candidates to run (R) and which to select blindly (U).

    Returns (run_list, unrun_list, est_cost_ms, static_cost_ms)."""
    static_cost = best_schedule(cands, ms)[1]
    order = sorted(cands, key=lambda t: -cpu_of(t, ms))
    n_heavy = sum(1 for t in order if cpu_of(t, ms) >= HEAVY_CPU or ms[t] >= HEAVY_CPU * 1000)
    ovh = (OVERHEAD_OVERLAY if overlay else OVERHEAD_HOT) if overhead is None else overhead
    best = (static_cost, len(order))
    suffix = [0.0] * (len(order) + 1)
    for i in range(len(order) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + cpu_of(order[i], ms) * mult
    for k in range(0, n_heavy + 1):
        if k == len(order):
            break
        U = order[:k]
        u_cost = best_schedule(U, ms)[1] if U else 0
        if can_diff and U:
            u_cost *= (1.0 - P_EQUIV)
        pen = FAIL_PENALTY * 1000 if not U else 500
        cost = u_cost + (suffix[k] + ovh) * 1000 + pen
        if cost < best[0]:
            best = (cost, k)
    k = best[1]
    U = order[:k]
    R = sorted(order[k:], key=lambda t: cpu_of(t, ms))
    return R, U, best[0], static_cost


READS_PATH = Path(os.environ.get("BR_READS", HERE / "reads.json.gz"))
NOISE_PATH = Path(os.environ.get("BR_NOISE", HERE / "noise.json"))
_READS = None


def load_reads():
    global _READS
    if _READS is None:
        import gzip
        try:
            with gzip.open(READS_PATH, "rt") as fh:
                R = json.load(fh)
            sets = [frozenset(x) for x in R["sets"]]
            byattr = {}
            for k, i in R["reads"].items():
                o, a, key = k.split("|", 2)
                byattr.setdefault((o, a), []).append((key, i))
            noise = {tuple(x) for x in json.loads(NOISE_PATH.read_text())}
            _READS = (sets, byattr, set(R["untraced"]), noise)
        except Exception as e:  # noqa
            log("reads map unavailable", repr(e))
            _READS = False
    return _READS


def _import_all():
    import importlib
    import pkgutil
    import sqlglot
    failed = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except BaseException as e:  # noqa
            failed.append((m.name, repr(e)[:80]))
    return failed


def overlay_candidates(j, old_mods, nodeids):
    """After re-importing the changed sqlglot: what changed in import-time state, and
    which tests read it."""
    broad = set(j["broad"])
    precise = set(j["precise"])
    R = load_reads()
    failed_imports = _import_all()
    old_failed = set(OLD_IMPORT_FAILURES)
    newly = [f for f in failed_imports if f[0] not in old_failed]
    if newly:
        return {"all": True, "note": f"import-fail {newly[0]}"}
    if not R:
        return {"cands": broad | precise, "note": "no-reads-map"}
    sets, byattr, untraced, noise = R
    import statediff
    new_mods = {k: v for k, v in sys.modules.items() if k == "sqlglot" or k.startswith("sqlglot.")}
    try:
        delta = [c for c in statediff.diff_state(old_mods, new_mods) if c not in noise]
    except Exception as e:  # noqa
        return {"cands": broad | precise, "note": f"diff-failed {e!r}"}
    cands = set(precise)
    unknown = []
    M = MAP_OBJ
    for kind, owner, attr, key in delta:
        if kind == "cls":
            if attr in ("*", "__mro__"):
                unknown.append((kind, owner, attr))
                continue
            for k2, i in byattr.get((owner, attr), []):
                if key is None or k2 == key or k2 == "*":
                    cands |= sets[i]
        elif kind in ("meth", "methcode"):
            if kind == "meth":
                for k2, i in byattr.get((owner, attr), []):
                    cands |= sets[i]
            if kind == "meth" or not precise:
                cands |= _function_coverage(old_mods, owner, attr, M)
        else:
            unknown.append((kind, owner, attr))
    note = f"delta={len(delta)} {[d[1].split(':')[-1] + '.' + str(d[2]) for d in delta][:3]}"
    if unknown:
        cands |= broad
        note += f" unknown={unknown[:2]}"
        if len(cands) > BROAD_CANDS:
            return {"all": True, "note": note + " broad"}
    if delta:
        cands |= untraced & (broad | precise)
    return {"cands": cands, "note": note, "unknown": bool(unknown)}


def _function_coverage(mods, owner, attr, M):
    modname, _, qual = owner.partition(":")
    mod = mods.get(modname)
    obj = mod
    try:
        for part in qual.split("."):
            obj = vars(obj)[part]
        fn = vars(obj)[attr]
        fn = getattr(fn, "__func__", fn)
        code = fn.__code__
        lines = [l for _, _, l in code.co_lines() if l]
        rel = os.path.relpath(code.co_filename, str(REPO))
        return M.covering(rel, min(lines), max(lines))
    except Exception:  # noqa
        return set()


MAP_OBJ = None
OLD_IMPORT_FAILURES = []


def run_jobs(jobs, answers, nodeids, ms, deadline):
    import runner

    modules = set()
    for j in jobs:
        for t in j["light"] + j["heavy"]:
            modules.add(runner.module_name_for(nodeids[t].split("::")[0]))
    runner.zygote_import(modules)
    if any(j["overlay"] for j in jobs):
        OLD_IMPORT_FAILURES[:] = [f[0] for f in _import_all()]
        load_reads()
    log(f"zygote ready at {time.time() - T_START:.2f}s")

    # cheapest jobs first so that a tight budget hurts the fewest commits
    def est(j):
        return sum(cpu_of(t, ms) for t in j["light"]) * 1000
    pending = sorted(jobs, key=est)
    running = {}  # pid -> (job, fd, started)
    tmp_root = tempfile.mkdtemp(prefix="br")
    total_est_left = sum(est(j) for j in pending) / 1000.0 * CPU_FACTOR

    while pending or running:
        now = time.time()
        # launch
        while pending and len(running) < PARALLEL:
            j = pending.pop(0)
            remaining = deadline - time.time()
            need = est(j) / 1000.0 * CPU_FACTOR
            total_est_left -= need
            # allow heavy runs only when the rest of the batch still fits
            heavy_budget = remaining - 3.0 - max(0.0, total_est_left) / PARALLEL
            light_cost = est(j) / 1000.0
            if remaining < 4.0 or light_cost > remaining - 3.0:
                log(j["cid"], "skipped (budget)")
                continue
            j["tmp"] = os.path.join(tmp_root, j["cid"])
            os.makedirs(j["tmp"], exist_ok=True)
            j["heavy_budget"] = heavy_budget
            rd, wr = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(rd)
                code = 0
                try:
                    out = child(j, nodeids, ms, runner, modules)
                except BaseException as e:  # noqa
                    out = {"error": repr(e)}
                try:
                    data = json.dumps(out).encode()
                    while data:
                        n = os.write(wr, data)
                        data = data[n:]
                finally:
                    os._exit(code)
            os.close(wr)
            running[pid] = (j, rd, time.time(), [])
        if not running:
            continue
        # collect
        fds = {v[1]: pid for pid, v in running.items()}
        timeout = max(0.05, deadline - time.time())
        r, _, _ = select.select(list(fds), [], [], min(timeout, 1.0))
        for fd in r:
            pid = fds[fd]
            j, _, started, buf = running[pid]
            chunk = os.read(fd, 65536)
            if chunk:
                buf.append(chunk)
                continue
            os.close(fd)
            os.waitpid(pid, 0)
            del running[pid]
            try:
                out = json.loads(b"".join(buf) or b"{}")
            except Exception:  # noqa
                out = {"error": "bad json"}
            if "error" in out or "failed" not in out:
                log(j["cid"], "child error", out.get("error"), "-> static")
                continue
            if out.get("all"):
                sel = list(range(len(nodeids)))
            else:
                sel = sorted(set(out["failed"]) | set(out.get("unrun", [])))
            answers[j["cid"]] = sel
            log(f"{j['cid']} light={len(j['light'])} heavy={len(j['heavy'])} failed={len(out['failed'])} "
                f"unrun={len(out.get('unrun', []))} {time.time() - started:.2f}s ov={int(j['overlay'])} {out.get('note', '')} | {j['reasons'][:80]}")
        if time.time() > deadline:
            for pid, (j, fd, started, buf) in list(running.items()):
                log(j["cid"], "killed at deadline -> static")
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except Exception:  # noqa
                    pass
                os.close(fd)
            running.clear()
            for j in pending:
                log(j["cid"], "not started -> static")
            pending = []


def child(j, nodeids, ms, runner, modules):
    os.environ["TMPDIR"] = j["tmp"]
    CPU_OVR.clear()
    CPU_OVR.update(j.get("cpu_ovr") or {})
    runner.SUBMIT_FILTER.clear()
    runner.SUBMIT_FILTER.update(j.get("subf") or {})
    runner.SUBMIT_LAZY.update(_submap())
    tempfile.tempdir = j["tmp"]
    if not j["overlay"]:
        for r in j["hot"]:
            if r.startswith("sqlglot/") or r.startswith("tests/"):
                name = r[:-3].replace("/", ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                try:
                    __import__(name)
                except Exception:  # noqa
                    pass
    note = ""
    old_mods = {k: v for k, v in sys.modules.items() if k == "sqlglot" or k.startswith("sqlglot.")}
    try:
        prepared = runner.prepare(j, modules)
    except runner.NeedReimport as e:
        j["overlay"] = True
        note = f"reimport({e})"
        prepared = runner.prepare(j, modules)
    unrun = list(j["heavy"])
    if j["overlay"]:
        res = overlay_candidates(j, old_mods, nodeids)
        if res.get("all"):
            return {"failed": [], "unrun": [], "all": True, "note": res["note"]}
        cands = sorted(res["cands"])
        j["unknown"] = res.get("unknown", False)
        note += " " + res["note"]
        if not cands:
            return {"failed": [], "unrun": [], "note": note}
        light, heavy, est, static = plan_split(cands, ms, CPU, True, False, overhead=0.0, mult=OV_MULT)
        budget = j.get("heavy_budget", 1e9)
        est_light = sum(cpu_of(t, ms) for t in light) * OV_MULT
        if est_light > min(budget, MAX_CHILD_CPU):
            light, heavy = [], cands
        note += f" cands={len(cands)} run={len(light)} est={est/1000:.1f}/{static/1000:.1f}"
        j["light"], j["heavy"] = light, heavy
        unrun = list(heavy)
    # light tests grouped by class (cheapest classes first) so class setup is shared
    groups = {}
    for t in j["light"]:
        groups.setdefault(nodeids[t].rsplit("::", 1)[0], []).append(t)
    ordered = [t for g in sorted(groups.values(), key=lambda g: sum(cpu_of(t, ms) for t in g)) for t in g]

    def should_stop(cur_failed, rest):
        if j["overlay"] and j.get("unknown") and len(cur_failed) >= BROAD_FAILS:
            return True
        base = sorted(set(unrun) | set(cur_failed))
        delta = best_schedule(base + rest, ms)[1] - best_schedule(base, ms)[1]
        return delta / 1000.0 <= sum(cpu_of(t, ms) for t in rest) + 0.2

    skipped = []
    failed = set(runner.run_tests(ordered, nodeids, timeouts=[max(10.0, ms[t] / 1000 * 5 + 5) for t in ordered],
                                  should_stop=should_stop, skipped=skipped))
    if skipped:
        unrun.extend(skipped)
        note += f" early-stop({len(skipped)})"
    if j["overlay"] and j.get("unknown") and len(failed) >= BROAD_FAILS:
        # an import-time change that breaks many tests: its reach is beyond what
        # the coverage map can tell, so run nothing more and select everything
        return {"failed": sorted(failed), "unrun": [], "all": True, "note": note + " broad-import-break"}
    if prepared is True:
        hits, bad = dict(runner.DIFF_HITS), set(runner.DIFF_BAD)
        n_sites = j["n_sites"]
        note += f" diff hits={hits} bad={sorted(bad)}"
        if unrun and not failed and hits and not bad and all(hits.get(k, 0) > 0 for k in range(n_sites)):
            note += " EQUIV->drop heavy"
            unrun = []
    return {"failed": sorted(failed), "unrun": unrun, "note": note}


if __name__ == "__main__":
    main()
