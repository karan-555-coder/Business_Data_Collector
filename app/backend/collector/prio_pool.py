"""ThreadPoolExecutor whose queued tasks run in priority order.

The crawl pool is always saturated on a small host (measured at 1 CPU: tasks
waited ~11 s in the queue), and a plain FIFO made Google Places listings
(~70% of them become a record) wait behind directory-mined links (~10%).
With priorities the target is reached with fewer crawls; whatever is still
queued at that point is cancelled. Equal priorities keep FIFO order.
"""

from __future__ import annotations

import heapq
import itertools
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

_LAST = float("inf")   # shutdown sentinels (None) after every real task


class _PriorityWorkQueue:
    """The subset of queue.SimpleQueue that ThreadPoolExecutor uses."""

    def __init__(self):
        self._heap: list = []
        self._seq = itertools.count()
        self._cv = threading.Condition(threading.Lock())
        self.next_prio = 0          # set by PriorityThreadPool.submit

    def put(self, item, block=True, timeout=None):
        prio = _LAST if item is None else self.next_prio
        with self._cv:
            heapq.heappush(self._heap, (prio, next(self._seq), item))
            self._cv.notify()

    def get(self, block=True, timeout=None):
        with self._cv:
            if not block:
                if not self._heap:
                    raise queue.Empty
            elif not self._cv.wait_for(lambda: self._heap, timeout):
                raise queue.Empty
            return heapq.heappop(self._heap)[2]

    def get_nowait(self):
        return self.get(block=False)

    def empty(self) -> bool:
        with self._cv:
            return not self._heap

    def qsize(self) -> int:
        with self._cv:
            return len(self._heap)


class PriorityThreadPool(ThreadPoolExecutor):
    """submit(fn, *args, prio=0): lower prio runs first."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._work_queue = _PriorityWorkQueue()
        self._prio_lock = threading.Lock()

    def submit(self, fn, /, *args, prio: float = 0, **kwargs):
        with self._prio_lock:      # next_prio is read by the put() inside
            self._work_queue.next_prio = prio
            return super().submit(fn, *args, **kwargs)
