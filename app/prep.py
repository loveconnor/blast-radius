"""Image build time: pickle the processed form of the committed maps (faster to load)."""
import os
import sys

os.environ["BR_PREP"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import selector  # noqa: E402
from selcore import Map  # noqa: E402

selector._pkl(selector.MAP, lambda: Map(selector.MAP))
selector.load_reads()
selector.load_outreads()
selector._replay()
selector._itermap()
selector.load_imports()
print("maps prepared", sorted(f for f in os.listdir(HERE) if f.endswith(".pkl")))
