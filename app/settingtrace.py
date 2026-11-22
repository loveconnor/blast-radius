"""Image-build instrumentation for scalar settings on SQLGlot dialects and generators."""


def install(record):
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect

    class Setting:
        def __init__(self, key, value):
            self.key = key
            self.value = value

        def __get__(self, obj, owner):
            record(self.key)
            return self.value

    dialects = set(Dialect.classes.values()) | {Dialect}
    classes = dialects | {base for dialect in dialects for base in dialect.generator_class.__mro__
                          if base.__module__.startswith("sqlglot.")}
    tracked = set()
    for cls in classes:
        for name, value in list(vars(cls).items()):
            if name.isupper() and (value is None or type(value) in (bool, int, float, str) or isinstance(value, exp.Expr)):
                key = (cls.__module__ + ":" + cls.__qualname__, name)
                tracked.add(key)
                setattr(cls, name, Setting(key, value))
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
