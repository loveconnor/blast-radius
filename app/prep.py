"""Image build time: pickle the processed form of the committed maps (faster to load)."""
import os
import sys

os.environ["BR_PREP"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import selector  # noqa: E402
from selcore import Map  # noqa: E402

selector._pkl(selector.MAP, lambda: Map(selector.MAP))
selector._submap()
selector.load_reads()
selector.load_outreads()
selector._replay()
selector._itermap()
selector.load_imports()
try:
    import runner  # noqa: E402
    runner.zygote_import({"tests.test_executor"})
    ok = runner.prebuild_class("tests.test_executor", "TestExecutor", cache=str(selector.PRESET_CACHE))
    print("executor fixtures cached:", ok)
except Exception as e:  # noqa
    print("executor fixtures not cached:", repr(e))
print("maps prepared", sorted(f for f in os.listdir(HERE) if f.endswith(".pkl")))
