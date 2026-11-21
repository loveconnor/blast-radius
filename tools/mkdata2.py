import json, glob, os, shutil, sys
lab, mut, out = sys.argv[1:4]
kinds = json.load(open(f'{mut}/kinds.json'))
shutil.rmtree(out, ignore_errors=True); os.makedirs(f'{out}/commits')
ans = {}
for f in sorted(glob.glob(f'{lab}/*.json')):
    c = os.path.basename(f)[:-5]
    if c not in kinds: continue
    d = json.load(open(f))
    if 'failed_nums' not in d: continue
    ans[c] = {'failing': d['failed_nums'], 'kind': kinds[c]}
    shutil.copy(f'{mut}/{c}.patch', f'{out}/commits/{c}.patch')
json.dump({'commits': ans}, open(f'{out}/answers.json', 'w'))
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sample', 'tests.json'), f'{out}/tests.json')
print(len(ans), 'commits;', sum(1 for a in ans.values() if a['failing']), 'with failures')
