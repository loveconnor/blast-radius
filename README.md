# Blast Radius: test selection for sqlglot patches

An entry for the RWX **Blast Radius** track. For every patch against sqlglot `eb11aec`,
it prints which tests to run and how to split them over up to 8 workers.

Nothing is looked up by patch. Every decision comes from the patch text and from maps
built from the pinned commit.

## How it decides

1. **Static reach** (`app/selcore.py`, `app/patchlib.py`).
   - The patch is applied in memory and diffed at the AST level.
   - Comment-only or formatting-only edits select nothing.
   - Each changed statement is classified as runtime (inside a function body), import-time, signature, or annotation.
   - Candidate tests come from a per-test line-coverage map of the pinned commit (`app/map.json.gz`). This covers test phases, class setup, pickled pool work and import-time lines.
   - Fixture and data files map to the tests that open them.
   - Module adds or removes, binary files, unparsable patches and import breakage select everything.
2. **Running the candidates cheaply** (`app/runner.py`, `app/selector.py`).
   - A zygote process imports sqlglot and all test modules once. Each commit then runs in a forked child.
   - Runtime-only edits are applied by swapping `__code__` on the live functions.
   - Edits that run at import time rebuild a symlink overlay and re-import. The old and new module and class state are then diffed structurally (`app/statediff.py`). Each changed attribute or dict key is mapped to the tests that read it, using an attribute-read trace of the pinned commit (`app/reads.json.gz`).
   - A cost model (`plan_split`) weighs running a test (container CPU) against selecting it unrun (scheduled seconds plus 2 s per worker). The expensive tail is selected without being run.
   - Runtime edits are instrumented with old/new differential probes. If the light tests pass and every probe saw identical values, the heavy tail is dropped.
   - TPC-H and TPC-DS execute only the queries whose coverage reaches the edited lines, and stop at the first failing query.
   - Tests stop early once the rest cannot change the answer's cost.
   - Every child has a deadline. If one does not finish, that commit falls back to the static candidate set.
3. **Schedule** (`app/sched.py`). LPT packing over 1–8 workers, picking the count that minimises the busiest worker plus 2 s per worker.

## Layout

- `Dockerfile`: python 3.13-slim, the locked requirements, and sqlglot at `eb11aec` from GitHub.
- `app/`: the selector and its prebuilt maps. All maps were built from the pinned commit.
- `tools/`: scripts that build the maps:
  - `covplugin.py` and `buildmap.py`: coverage
  - `importcov.py`: import-time lines
  - `attrtrace.py` and `buildreads.py`: attribute reads
  - `percpu.py`: per-test CPU
  - `mknoise.py`: state-diff noise
  - `buildsub.py`: per-query coverage
  - `genmutants.py`, `fastlabel.py` and friends: evaluation on synthetic mutants
- `make sample` runs the image under the scorer's flags on the sample and scores it.

The tools expect a dev container with this repo at `/w`, the sqlglot checkout at `/w/sqlglot`, and the starter kit at `/kit`.
