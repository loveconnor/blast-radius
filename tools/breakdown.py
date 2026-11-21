import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sched import best_schedule
data, out = sys.argv[1], sys.argv[2]
A = json.load(open(f'{data}/answers.json'))['commits']
T = json.load(open(f'{data}/tests.json'))['tests']; ms = [round(t['seconds']*1000) for t in T]
rows = []
for line in open(out):
    d = json.loads(line); c = d['commit']; sel = {t for w in d['workers'] for t in w}
    cost = best_schedule(sorted(sel), ms)[1] if sel else 0
    F = set(A[c]['failing']); orc = best_schedule(sorted(F), ms)[1] if F else 0
    miss = bool(F - sel)
    rows.append((cost - orc, c, A[c]['kind'], cost/1000, orc/1000, len(sel), len(F), miss))
rows.sort(reverse=True)
tot = sum(r[3] for r in rows); otot = sum(r[4] for r in rows)
print(f'total sim {tot:.1f} oracle {otot:.1f} n={len(rows)}')
import collections
byk = collections.defaultdict(lambda: [0, 0, 0])
for r in rows: byk[r[2]][0] += r[3]; byk[r[2]][1] += r[4]; byk[r[2]][2] += 1
print({k: (round(v[0], 1), round(v[1], 1), v[2]) for k, v in byk.items()})
for r in rows[:25]: print(f'{r[1]} {r[2]:8} cost={r[3]:6.2f} oracle={r[4]:6.2f} sel={r[5]} fail={r[6]} {"MISS" if r[7] else ""}')
