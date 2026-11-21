import heapq

def lpt(sorted_items, k, want_workers=True):
    """sorted_items: list of (ms, test) pre-sorted descending."""
    if not want_workers:
        if k == 1:
            return sum(ms for ms, _ in sorted_items)
        h = [0] * k
        for ms, _ in sorted_items:
            heapq.heapreplace(h, h[0] + ms)
        return max(h)
    if k == 1:
        return [[t for _, t in sorted_items]], sum(ms for ms, _ in sorted_items)
    h = [[0, i] for i in range(k)]
    ws = [[] for _ in range(k)]
    for ms, t in sorted_items:
        top = h[0]
        i = top[1]
        ws[i].append(t)
        top[0] += ms
        heapq.heapreplace(h, top)
    return ws, max(load for load, _ in h)

def best_schedule(tests, ms_of, want_workers=True):
    """choose worker count minimising makespan + 2000ms per worker."""
    if not tests:
        return ([], 0) if want_workers else (None, 0)
    items = sorted(((ms_of[t], t) for t in tests), reverse=True)
    best_c = 10**18
    best_k = 1
    max_k = min(8, len(items))
    for k in range(1, max_k + 1):
        mk = lpt(items, k, want_workers=False)
        c = mk + 2000 * k
        if c < best_c:
            best_c = c
            best_k = k
    if not want_workers:
        return None, best_c
    ws, _ = lpt(items, best_k, want_workers=True)
    return ws, best_c
