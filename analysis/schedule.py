"""
FAST Team - Worker scheduling
Distributes tests across workers to minimize total cost.
"""
from typing import Dict, List, Set

WORKER_COST_MS = 2000  # 2 seconds per worker
MAX_WORKERS = 8


def split(selected: Set[int], runtimes: Dict[int, int]) -> List[List[int]]:
    """
    Distribute tests across workers minimizing: max_load + 2s * num_workers

    Uses LPT (Longest Processing Time) algorithm for bin packing.
    """
    if not selected:
        return []

    # Sort by runtime descending (largest first)
    items = [(n, runtimes.get(n, 1000)) for n in selected]
    items.sort(key=lambda x: (-x[1], x[0]))

    best_cost = float("inf")
    best_result: List[List[int]] = []

    # Try different worker counts
    for k in range(1, min(MAX_WORKERS + 1, len(items) + 1)):
        loads = [0] * k
        buckets: List[List[int]] = [[] for _ in range(k)]

        # Assign each test to least loaded worker
        for num, ms in items:
            min_idx = 0
            min_load = loads[0]
            for i in range(1, k):
                if loads[i] < min_load:
                    min_load = loads[i]
                    min_idx = i

            buckets[min_idx].append(num)
            loads[min_idx] += ms

        # Calculate cost
        cost = max(loads) + WORKER_COST_MS * k

        if cost < best_cost:
            best_cost = cost
            best_result = [b for b in buckets if b]

    return best_result
