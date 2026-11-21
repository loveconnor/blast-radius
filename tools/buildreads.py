"""attrreads.json -> compact reads map: entry 'owner|attr|key' -> set index."""
import json, gzip, sys
raw = json.load(open(sys.argv[1])); out = sys.argv[2]
reads = {}
bad = []
for t, v in raw.items():
    t = int(t)
    if v["failed"] or v["reads"] is None:
        bad.append(t); continue
    for owner, attr, key in v["reads"]:
        k = f"{owner}|{attr}|{'' if key is None else key}"
        reads.setdefault(k, set()).add(t)
sets, idx, m = [], {}, {}
for k, s in reads.items():
    fs = tuple(sorted(s))
    if fs not in idx:
        idx[fs] = len(sets); sets.append(list(fs))
    m[k] = idx[fs]
with gzip.open(out, "wt") as fh:
    json.dump({"sets": sets, "reads": m, "untraced": sorted(bad)}, fh, separators=(",", ":"))
print(len(m), "entries", len(sets), "sets", "untraced", bad, file=sys.stderr)
