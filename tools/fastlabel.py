"""Label patches by running the full suite in forked in-process children."""
import json, os, sys, time, tempfile, select
from pathlib import Path
sys.path.insert(0, "/w/tools"); os.environ["BR_REPO"] = "/w/sqlglot"
from selcore import Map, analyze
import runner
outdir, par = sys.argv[1], int(sys.argv[2]); patches = sys.argv[3:]
M = Map("/w/out/map.json.gz"); repo = Path("/w/sqlglot")
tests = json.load(open("/kit/sample/tests.json"))["tests"]; nodeids = [t["id"] for t in tests]
allt = list(range(len(tests)))
mods = {runner.module_name_for(t["file"]) for t in tests}
runner.zygote_import(mods)
todo = [p for p in patches if not os.path.exists(os.path.join(outdir, Path(p).stem + ".json"))]
running = {}
while todo or running:
    while todo and len(running) < par:
        pf = todo.pop(0); cid = Path(pf).stem
        D = analyze(open(pf).read(), repo, M)
        hot = [r for r, c in D.files.items() if r.endswith(".py") and c is not None and (repo / r).exists()]
        job = dict(files=D.files, olds={r: (repo / r).read_text() for r in hot}, hot=hot,
                   overlay=bool(D.import_change or D.all), tmp=tempfile.mkdtemp())
        rd, wr = os.pipe(); pid = os.fork()
        if pid == 0:
            os.close(rd)
            try:
                if not job["overlay"]:
                    for r in hot:
                        if r.startswith("sqlglot/"):
                            try: __import__(r[:-3].replace("/", ".").removesuffix(".__init__"))
                            except Exception: pass
                try:
                    runner.prepare(job, mods)
                except runner.NeedReimport:
                    job["overlay"] = True; runner.prepare(job, mods)
                f = runner.run_tests(allt, nodeids, timeouts=[120.0] * len(allt))
                out = {"failed_nums": sorted(f), "overlay": job["overlay"]}
            except BaseException as e:
                out = {"error": repr(e)}
            os.write(wr, json.dumps(out).encode()); os._exit(0)
        os.close(wr); running[rd] = (pid, cid, time.time(), [])
    r, _, _ = select.select(list(running), [], [], 5)
    for fd in r:
        pid, cid, t0, buf = running[fd]
        ch = os.read(fd, 1 << 20)
        if ch: buf.append(ch); continue
        os.close(fd); os.waitpid(pid, 0); del running[fd]
        out = json.loads(b"".join(buf) or b'{"error":"empty"}'); out["secs"] = round(time.time() - t0, 1)
        json.dump(out, open(os.path.join(outdir, cid + ".json"), "w"))
        print(cid, len(out.get("failed_nums", [])), out["secs"], out.get("error", ""), flush=True)
