"""Record which TPC fixture reads each scalar dialect setting on the pinned checkout."""
import os
import pickle
import sys

import runner
sys.path.insert(0, runner.REPO)
from sqlglot.dialects.dialect import Dialect


class Setting:
    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __get__(self, obj, owner):
        reads[self.key] = reads.get(self.key, 0) | current
        return self.value


def switch(index, at_parse=False):
    global current
    current = 1 if index is None else 2 << index


ids = ["tests/test_executor.py::TestExecutor::test_execute_" + suffix for suffix in ("tpcds", "tpch")]
runner.zygote_import({"tests.test_executor"})
# Force dialect registration before installing descriptors on the concrete owners.
classes = set(Dialect.classes.values()) | {Dialect}
tracked = set()
for cls in classes:
    for name, value in list(vars(cls).items()):
        if name.isupper() and (value is None or type(value) in (bool, str, int, float)):
            key = (f"{cls.__module__}:{cls.__qualname__}", name)
            setattr(cls, name, Setting(key, value))
            tracked.add(key)

runner.SUBMIT_LAZY = {0, 1}
runner.MEMO_IDS = dict(enumerate(ids))
runner.MEMO_DUCK = True
runner.QUERY_TRACE = switch
out = {}
for i, tid in enumerate(ids):
    # Independent class setup and cold test caches for each test.
    path = sys.argv[1] + ".part"
    pid = os.fork()
    if pid == 0:
        reads = {}
        switch(None)
        failed = runner.run_tests([i], ids, timeouts=[300.0])
        if not failed:
            with open(path, "wb") as fh:
                pickle.dump(reads, fh)
        os._exit(int(bool(failed)))
    _, status = os.waitpid(pid, 0)
    if status == 0:
        with open(path, "rb") as fh:
            out[tid] = pickle.load(fh)
        os.unlink(path)
with open(sys.argv[1], "wb") as fh:
    pickle.dump((tracked, out), fh)
print("TPC scalar-setting trace", len(tracked), {k: len(v) for k, v in out.items()})
