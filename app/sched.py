def lpt(items, k):
    """items: list of (ms, test). returns workers lists and makespan."""
    loads = [0] * k; ws = [[] for _ in range(k)]
    for ms, t in sorted(items, reverse=True):
        i = loads.index(min(loads)); ws[i].append(t); loads[i] += ms
    return ws, max(loads)

def best_schedule(tests, ms_of):
    """choose worker count minimising makespan + 2000ms per worker."""
    if not tests: return [], 0
    items = [(ms_of[t], t) for t in tests]
    best = None
    for k in range(1, min(8, len(items)) + 1):
        ws, mk = lpt(items, k)
        # local improvement could go here
        c = mk + 2000 * k
        if best is None or c < best[1]: best = (ws, c)
    return best
