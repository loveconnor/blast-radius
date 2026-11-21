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

def solve_partition(items, k):
    """Optimal partition of items across k workers.
    items: list of (ms, test) pre-sorted descending."""
    durations = [ms for ms, _ in items]
    n = len(durations)
    if k == 1:
        return [[t for _, t in items]], sum(durations)
    if k >= n:
        return [[t] for _, t in items] + [[] for _ in range(k - n)], max(durations)
    
    total = sum(durations)
    lb = max(durations[0], (total + k - 1) // k)
    ub_ws, ub = lpt(items, k, want_workers=True)
    if ub == lb:
        return ub_ws, ub
    
    # Exact branch-and-bound for small test counts (<= 18)
    if n <= 18:
        best_load = [ub]
        best_assign = [ub_ws]
        loads = [0] * k
        assign = [[] for _ in range(k)]
        
        def search(idx):
            if idx == n:
                m = max(loads)
                if m < best_load[0]:
                    best_load[0] = m
                    best_assign[0] = [list(w) for w in assign]
                return
            
            d = durations[idx]
            t = items[idx][1]
            seen_loads = set()
            for i in range(k):
                load = loads[i]
                if load in seen_loads or load + d >= best_load[0]:
                    continue
                seen_loads.add(load)
                loads[i] += d
                assign[i].append(t)
                search(idx + 1)
                assign[i].pop()
                loads[i] -= d
                if load == 0:
                    break
        search(0)
        return best_assign[0], best_load[0]

    # 2-opt local search improvement on top of LPT for larger test counts
    ws = [list(w) for w in ub_ws]
    t_ms = {t: ms for ms, t in items}
    loads = [sum(t_ms[t] for t in w) for w in ws]
    best_max = max(loads)
    improved = True
    iterations = 0
    while improved and iterations < 50:
        improved = False
        iterations += 1
        max_idx = loads.index(best_max)
        for t in list(ws[max_idx]):
            ms = t_ms[t]
            for j in range(k):
                if j != max_idx and loads[j] + ms < best_max:
                    ws[max_idx].remove(t)
                    ws[j].append(t)
                    loads[max_idx] -= ms
                    loads[j] += ms
                    best_max = max(loads)
                    improved = True
                    break
            if improved:
                break
        if not improved:
            for t1 in list(ws[max_idx]):
                ms1 = t_ms[t1]
                for j in range(k):
                    if j == max_idx:
                        continue
                    for t2 in list(ws[j]):
                        ms2 = t_ms[t2]
                        diff = ms1 - ms2
                        if 0 < diff < (best_max - loads[j]):
                            if max(loads[max_idx] - diff, loads[j] + diff) < best_max:
                                ws[max_idx].remove(t1)
                                ws[j].remove(t2)
                                ws[max_idx].append(t2)
                                ws[j].append(t1)
                                loads[max_idx] -= diff
                                loads[j] += diff
                                best_max = max(loads)
                                improved = True
                                break
                    if improved:
                        break
                if improved:
                    break
    return ws, best_max

def best_schedule(tests, ms_of, want_workers=True):
    """choose worker count minimising makespan + 2000ms per worker."""
    if not tests:
        return ([], 0) if want_workers else (None, 0)
    items = sorted(((ms_of[t], t) for t in tests), reverse=True)
    best_c = 10**18
    best_k = 1
    max_k = min(8, len(items))
    if not want_workers:
        for k in range(1, max_k + 1):
            mk = lpt(items, k, want_workers=False)
            c = mk + 2000 * k
            if c < best_c:
                best_c = c
                best_k = k
        return None, best_c

    best_ws = None
    for k in range(1, max_k + 1):
        ws, mk = solve_partition(items, k)
        c = mk + 2000 * k
        if c < best_c:
            best_c = c
            best_k = k
            best_ws = ws
    return best_ws, best_c
