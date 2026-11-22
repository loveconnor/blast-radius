"""Image-build instrumentation for scalar settings on SQLGlot dialects and their parser, tokenizer and generator classes."""


# These two dialect strings are read eagerly by Generator.__init__ to fill
# private slots. Reading either slot later observes the original setting.
ESCAPE_SLOTS = {"BYTE_END": "_escaped_byte_quote_end", "QUOTE_END": "_escaped_quote_end"}


def effective(key):
    return key[0], "read:" + key[1]


def install(record, escaped=False, schemas=False):
    import sys
    from sqlglot import exp
    from sqlglot.generator import Generator
    from sqlglot.dialects.dialect import Dialect

    class Setting:
        def __init__(self, key, value):
            self.key = key
            self.value = value

        def __get__(self, obj, owner):
            record(self.key)
            return self.value

    class EscapeSetting(Setting):
        def __get__(self, obj, owner):
            record(self.key)
            if owner not in dialects or sys._getframe(1).f_code is not constructor:
                record(effective(self.key))
            return self.value

    class EscapeSlot:
        def __init__(self, slot, name):
            self.slot = slot
            self.name = name

        def __get__(self, obj, owner):
            record(("generator-slot", self.name))
            return self.slot.__get__(obj, owner)

        def __set__(self, obj, value):
            self.slot.__set__(obj, value)

        def __delete__(self, obj):
            self.slot.__delete__(obj)

    constructor = Generator.__init__.__code__
    if escaped:
        for name, slot in ESCAPE_SLOTS.items():
            setattr(Generator, slot, EscapeSlot(vars(Generator)[slot], name))

    dialects = set(Dialect.classes.values()) | {Dialect}
    families = {impl for dialect in dialects
                for impl in (dialect.generator_class, dialect.parser_class, dialect.tokenizer_class)}
    classes = dialects | {base for impl in families for base in impl.__mro__
                          if base.__module__.startswith("sqlglot.")}
    tracked = set()
    for cls in classes:
        for name, value in list(vars(cls).items()):
            if name.isupper() and (value is None or type(value) in (bool, int, float, str) or isinstance(value, exp.Expr)):
                key = (cls.__module__ + ":" + cls.__qualname__, name)
                tracked.add(key)
                if escaped and cls in dialects and name in ESCAPE_SLOTS:
                    tracked.update((effective(key), ("generator-slot", name)))
                    setattr(cls, name, EscapeSetting(key, value))
                else:
                    setattr(cls, name, Setting(key, value))
    if schemas:
        # Each subclass has its own required_args set. An inherited arg_types
        # access resolves to the descriptor on the class defining that dict.
        seen = set()
        pending = [exp.Expr]
        while pending:
            cls = pending.pop()
            if cls in seen:
                continue
            seen.add(cls)
            pending.extend(cls.__subclasses__())
            if not cls.__module__.startswith("sqlglot.expressions"):
                continue
            for attr, typ in (("arg_types", dict), ("required_args", set)):
                value = vars(cls).get(attr)
                if type(value) is typ:
                    key = (cls.__module__ + ":" + cls.__qualname__, attr)
                    tracked.add(key)
                    setattr(cls, attr, Setting(key, value))
    return tracked


def install_routes(record):
    """Report the concrete lookup, including uses by custom generator classes."""
    from sqlglot import generator
    from dispatch import generator_classes, name

    known = generator_classes()
    build = generator._build_dispatch

    class ObservedDispatch(dict):
        def get(self, expression, default=None):
            key = (self.owner, name(expression)) if self.owner is not None else (None, None)
            record(key)
            return super().get(expression, default)

    def observed(cls):
        table = ObservedDispatch(build(cls))
        table.owner = name(cls) if cls in known else None
        return table

    generator._build_dispatch = observed
    generator._DISPATCH_CACHE.clear()
    return {name(cls) for cls in known}


def query_keys(settings):
    """Use deferred reads only if every builtin constructor can form its string."""
    from sqlglot.dialects.dialect import Dialect
    import dispatch

    out = set(settings)
    dialects = set(Dialect.classes.values()) | {Dialect}
    for owner, attr in settings:
        if attr not in ESCAPE_SLOTS:
            continue
        affected = [d for d in dialects if any(dispatch.name(base) == owner for base in d.__mro__)]
        if not affected:
            continue
        for d in affected:
            value = getattr(d, attr)
            if attr == "BYTE_END" and value is None:
                continue
            escapes = d.tokenizer_class.STRING_ESCAPES
            if type(value) is not str or not escapes or type(escapes[0]) is not str:
                break
        else:
            out.remove((owner, attr))
            out.update((effective((owner, attr)), ("generator-slot", attr)))
    return out
