import sys, json, os, pkgutil, importlib
SRC="/src"; lines={}
mon=sys.monitoring
def _line(code, line):
    fn=code.co_filename
    if fn.startswith(SRC): lines.setdefault(fn,set()).add(line)
    return mon.DISABLE
mon.use_tool_id(3,"imp"); mon.register_callback(3,mon.events.LINE,_line); mon.set_events(3,mon.events.LINE)
import sqlglot
bad=[]
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try: importlib.import_module(m.name)
    except Exception as e: bad.append((m.name,repr(e)))
from sqlglot.dialects.dialect import Dialect, Dialects
for d in Dialects:
    try:
        D=Dialect.get_or_raise(d.value)
        D.tokenizer_class; D.parser_class; D.generator_class
        D().tokenize("select 1"); D().parse("select 1"); D().generate(D().parse("select 1")[0])
    except Exception as e: bad.append((d.value,repr(e)))
mon.set_events(3,0)
print(bad, file=sys.stderr)
json.dump({f:sorted(v) for f,v in lines.items()}, open("/out/importcov.json","w"))
