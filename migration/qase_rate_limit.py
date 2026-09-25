"""
Qase API rate limiting (thread-safe sliding window).

Use one instance per API account/token when source and target are different
workspaces (separate 1000 req/min quotas).
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque


class QaseApiRateLimiter:
    """
    At most `max_calls` API calls per `window_seconds` (rolling window).
    Blocks in acquire() until slots are available.
    """

    def __init__(self, max_calls: int = 1000, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._call_times: deque[float] = deque()

    def acquire(self, n: int = 1) -> None:
        for _ in range(max(1, n)):
            self._acquire_one()

    def _acquire_one(self) -> None:
        while True:
            sleep_s = 0.0
            with self._lock:
                now = time.monotonic()
                while self._call_times and now - self._call_times[0] >= self.window_seconds:
                    self._call_times.popleft()
                if len(self._call_times) < self.max_calls:
                    self._call_times.append(now)
                    return
                oldest = self._call_times[0]
                sleep_s = max(0.05, self.window_seconds - (now - oldest) + 0.05)
            time.sleep(min(sleep_s, 5.0))


def exponential_backoff_delay(
    attempt: int,
    base_delay: float = 1.5,
    max_delay: float = 90.0,
    jitter_fraction: float = 0.15,
) -> float:
    """Capped exponential backoff with small jitter (attempt is 0-based)."""
    raw = base_delay * (2**attempt)
    cap = min(raw, max_delay)
    jitter = cap * jitter_fraction * random.random()
    return min(cap + jitter, max_delay)
