"""Label mutant patches by running the full test suite. Runs inside the dev container.
usage: python label.py <outdir> patch...
"""
import json, os, shutil, subprocess, sys, time
sys.path.insert(0, "/w/tools")
from patchlib import parse_patch, apply_hunks
outdir = sys.argv[1]
for pf in sys.argv[2:]:
    cid = os.path.basename(pf)[:-6]
    res = os.path.join(outdir, cid + ".json")
    if os.path.exists(res):
        continue
    work = f"/tmp/m_{cid}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree("/w/sqlglot", work, ignore=shutil.ignore_patterns(".git", "__pycache__", "sqlglot-integration-tests"))
    for fp in parse_patch(open(pf).read()):
        old = os.path.join(work, fp.old_path) if fp.old_path else None
        txt = open(old).read() if old and os.path.exists(old) else None
        new = apply_hunks(txt, fp)
        dst = os.path.join(work, fp.path)
        if new is None:
            os.remove(dst)
        else:
            open(dst, "w").write(new)
    t = time.time()
    for attempt in range(3):
        env = dict(os.environ, PYTHONPATH=work + ":/w/tools", FAIL_OUT=res + ".tmp", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-x" if False else "-q", "-p", "no:cacheprovider", "-p", "failplugin",
                        "--timeout=300", "tests"], cwd=work, env=env, stdout=open(res + ".log", "w"), stderr=subprocess.STDOUT)
        if os.path.exists(res + ".tmp"): break
    if not os.path.exists(res + ".tmp"):
        print(cid, "FAILED-TO-LABEL", flush=True); continue
    d = json.load(open(res + ".tmp")); d["secs"] = round(time.time() - t, 1)
    json.dump(d, open(res, "w")); os.remove(res + ".tmp")
    shutil.rmtree(work, ignore_errors=True)
    print(cid, len(d["failed"]), d["secs"], flush=True)
