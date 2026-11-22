"""Observe dialect attribute receivers in an upstream pytest process and its workers."""
import dis
import json
import mmap
import os
import sys
from pathlib import Path


def install(directory, repo):
    directory = Path(directory)
    sites = json.loads((directory / "sites.json").read_text())
    with (directory / "current").open("rb") as f:
        current = mmap.mmap(f.fileno(), 4, access=mmap.ACCESS_READ)
    touched = set()
    offsets = {}

    def mark(tag):
        (directory / f"{tag}.{os.getpid()}").touch()

    mark("boot")
    prefix = str(repo) + "/"
    mon = sys.monitoring

    def start(code, _):
        if code.co_filename.startswith(prefix):
            wanted = sites.get(code.co_filename[len(prefix):])
            if wanted:
                targets = {}
                previous = None
                for ins in dis.get_instructions(code):
                    if ins.opname == "LOAD_ATTR":
                        site = f"{ins.positions.lineno}|{ins.argval}"
                        if (site in wanted and previous is not None and
                                previous.opname in ("LOAD_FAST", "LOAD_FAST_CHECK")):
                            targets[ins.offset] = wanted[site], previous.argval, ins.argval
                    previous = ins
                if targets:
                    offsets[code] = targets
                    mon.set_local_events(4, code, mon.events.INSTRUCTION)
        return mon.DISABLE

    def instruction(code, offset):
        target = offsets[code].get(offset)
        if target is None:
            return mon.DISABLE
        site, local, attr = target
        obj = sys._getframe(1).f_locals.get(local)
        cls = type(obj)
        dialect_module = sys.modules.get("sqlglot.dialects.dialect")
        base = vars(dialect_module).get("Dialect") if dialect_module else None
        module = sys.modules.get(cls.__module__)
        valid = (base and issubclass(cls, base) and cls.__module__.startswith("sqlglot.dialects.")
                 and module and vars(module).get(cls.__name__) is cls and attr not in vars(obj))
        test = int.from_bytes(current[:], "little", signed=True)
        tag = f"{'seen' if valid else 'unsafe'}-{site}-{test}"
        if tag not in touched:
            mark(tag)
            touched.add(tag)

    mon.use_tool_id(4, "dialect-attribute-receivers")
    mon.register_callback(4, mon.events.PY_START, start)
    mon.register_callback(4, mon.events.INSTRUCTION, instruction)
    mon.set_events(4, mon.events.PY_START)

    # Spawn starts a fresh interpreter. Only accept batches in which every
    # scheduled worker loaded this hook, including its cold module imports.
    from multiprocessing.process import BaseProcess
    start_process = BaseProcess.start

    def process_start(self):
        start_process(self)
        (directory / f"worker.{self.pid}").touch()

    BaseProcess.start = process_start

    def audit(event, args):
        if event in ("os.fork", "os.posix_spawn", "subprocess.Popen"):
            mark("escaped")

    sys.addaudithook(audit)
