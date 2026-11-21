import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from selcore import Map, analyze
data = Path(sys.argv[1]); answers = json.load(open(sys.argv[2]))["commits"]
M = Map("out/map.json.gz"); repo = Path("sqlglot")
tests = json.load(open(data / "tests.json"))["tests"]; sec = [t["seconds"] for t in tests]
tot_miss = 0
for pf in sorted((data / "commits").glob("*.patch")):
    c = pf.stem; D = analyze(pf.read_text(), repo, M); fail = set(answers[c]["failing"])
    cands = set(range(M.n)) if D.all else D.cands
    miss = fail - cands
    tot_miss += bool(miss)
    print(f"{c} {answers[c]['kind']:8} fail={len(fail):3} cand={len(cands):4} t={sum(sec[i] for i in cands):7.2f} imp={int(D.import_change)} {'MISS '+str(sorted(miss)) if miss else ''} {' '.join(D.reasons)[:150]}")
print("missed commits", tot_miss)
