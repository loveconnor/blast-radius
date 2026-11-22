"""Build immutable generator inputs whose parser and generator digests can both be reproduced."""
import gzip
import json
import pickle
import sys
import zlib
from pathlib import Path

import replay
import runner

runner.zygote_import(set())
path = Path(__file__).with_name("replay.json.gz")
with gzip.open(path, "rt") as fh:
    data = json.load(fh)
keys = {replay._pkey(key): i for i, key in enumerate(data["keys"]) if key[0] != "g"}
sources = {}
for i, key in enumerate(data["keys"]):
    if key[0] == "g" and key[6][0] != "s":
        src, position = key[6]
        source = keys.get(replay._pkey(src))
        if source is not None:
            sources.setdefault(source, {}).setdefault(position, []).append(i)

entries = {}
replay.begin_batch()
with replay.TREE_DATA.open("wb") as fh:
    for source, positions in sources.items():
        key = data["keys"][source]
        try:
            actual, trees, error = replay.observe(lambda: replay._parse_key(key))
            if error is not None or actual != data["dig"][source]:
                continue
        except (ValueError, TypeError, RecursionError):
            continue
        for position, consumers in positions.items():
            try:
                raw = pickle.dumps(replay.dump_tree(trees[position]), protocol=5)
                blob = zlib.compress(raw, 1)
                verified = []
                for i in consumers:
                    result = replay.generate(data["keys"][i], replay.load_tree(pickle.loads(raw)))
                    if result == data["dig"][i]:
                        verified.append(i)
                if verified:
                    record = source, fh.tell(), len(blob)
                    fh.write(blob)
                    for i in verified:
                        entries[i] = record
            except Exception:
                continue
with replay.TREE_INDEX.open("wb") as fh:
    pickle.dump(entries, fh, protocol=5)
print("verified generator input trees", len(entries), file=sys.stderr)
