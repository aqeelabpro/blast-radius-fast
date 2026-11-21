"""
FAST Team - Worker scheduling
Distributes required tests across workers and uses otherwise-free worker capacity
to run additional nearby tests without increasing the simulated makespan.
"""
from typing import Dict, List, Optional, Set

WORKER_COST_MS = 2000  # 2 seconds per worker
MAX_WORKERS = 8


def _affinity(test_number: int, worker: List[int], test_files: Dict[int, str]) -> int:
    """Rank extra tests by proximity to tests already assigned to this worker."""
    path = test_files.get(test_number)
    if not path:
        return 3

    worker_paths = [test_files.get(number) for number in worker]
    if path in worker_paths:
        return 0

    parent = path.rpartition("/")[0]
    if parent and any(p and p.rpartition("/")[0] == parent for p in worker_paths):
        return 1

    parts = path.split("/")
    if len(parts) >= 2 and any(
        p and len(p.split("/")) >= 2 and p.split("/")[:2] == parts[:2]
        for p in worker_paths
    ):
        return 2

    return 3


def split(
    selected: Set[int],
    runtimes: Dict[int, int],
    universe: Optional[Set[int]] = None,
    test_files: Optional[Dict[int, str]] = None,
) -> List[List[int]]:
    """
    Minimize max_worker_load + 2s * worker_count with LPT scheduling.

    Once the best required-test schedule is chosen, pack additional tests into
    its unused worker capacity. An extra test is admitted only when its runtime
    keeps that worker at or below the original makespan, so the score cannot
    increase. Nearby test files are preferred, then shorter/global candidates.
    """
    if not selected:
        return []

    items = [(n, runtimes.get(n, 1000)) for n in selected]
    items.sort(key=lambda x: (-x[1], x[0]))

    best_cost = float("inf")
    best_result: List[List[int]] = []
    best_loads: List[int] = []

    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        buckets: List[List[int]] = [[] for _ in range(k)]

        for num, ms in items:
            min_idx = min(range(k), key=lambda i: (loads[i], i))
            buckets[min_idx].append(num)
            loads[min_idx] += ms

        cost = max(loads) + WORKER_COST_MS * k
        if cost < best_cost:
            best_cost = cost
            best_result = buckets
            best_loads = loads

    if universe is None:
        return best_result

    # Keep the chosen schedule's makespan fixed. Extra tests are free only
    # while every worker remains within this original maximum load.
    peak = max(best_loads)
    assigned = {number for worker in best_result for number in worker}
    candidates = set(universe) - assigned
    if not candidates:
        return best_result

    files = test_files or {}
    original_paths = [files.get(number) for number in assigned]

    def candidate_priority(number: int):
        path = files.get(number)
        if not path:
            affinity = 3
        elif path in original_paths:
            affinity = 0
        else:
            parent = path.rpartition("/")[0]
            if parent and any(p and p.rpartition("/")[0] == parent for p in original_paths):
                affinity = 1
            else:
                parts = path.split("/")
                affinity = 2 if len(parts) >= 2 and any(
                    p and len(p.split("/")) >= 2 and p.split("/")[:2] == parts[:2]
                    for p in original_paths
                ) else 3
        return affinity, -runtimes.get(number, 1000), number

    for number in sorted(candidates, key=candidate_priority):
        ms = runtimes.get(number, 1000)
        choices = []
        for index, worker in enumerate(best_result):
            slack = peak - best_loads[index]
            if ms <= slack:
                choices.append((
                    _affinity(number, worker, files),
                    slack - ms,
                    index,
                ))
        if choices:
            _, _, index = min(choices)
            best_result[index].append(number)
            best_loads[index] += ms

    return best_result
