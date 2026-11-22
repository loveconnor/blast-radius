"""Build-time coverage of both executed lines and one-line Python callables."""


def register(monitor, tool, line):
    def enter(code, offset):
        return line(code, code.co_firstlineno)

    events = monitor.events
    monitor.register_callback(tool, events.LINE, line)
    monitor.register_callback(tool, events.PY_START, enter)
    monitor.register_callback(tool, events.PY_RESUME, enter)
    return events.LINE | events.PY_START | events.PY_RESUME
