"""In-memory login throttling, per client IP and per email.

State lives in this process, which is correct while one API process serves
traffic. With several processes each would count separately, so move the
counters to a shared store before scaling out. Attempts are counted per
window, before they are verified; the per-email counter clears on a successful login.

Both limits count unknown emails too, so a lockout never reveals whether an
account exists. A per-email limit lets someone lock a known address out for
the window; that trade is deliberate and the window is short.
"""

import time
from collections import deque
from collections.abc import Callable

# Bounds memory under a flood of distinct keys.
MAX_TRACKED_KEYS = 10_000


class SlidingWindowCounter:
    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float]) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            return deque()
        cutoff = self._clock() - self._window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            del self._events[key]
            return deque()
        return events

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may try again; 0 if it may try now."""
        events = self._recent(key)
        if len(events) < self._limit:
            return 0
        return max(1, int(events[0] + self._window - self._clock()) + 1)

    def record(self, key: str) -> None:
        if key not in self._events and len(self._events) >= MAX_TRACKED_KEYS:
            self._evict_oldest()
        self._events.setdefault(key, deque()).append(self._clock())

    def clear(self, key: str) -> None:
        self._events.pop(key, None)

    def _evict_oldest(self) -> None:
        oldest = min(self._events, key=lambda k: self._events[k][-1])
        del self._events[oldest]


class LoginRateLimiter:
    def __init__(
        self,
        *,
        # Generous because behind the Compose network every client can share one
        # address (see ADR 0002); the per-email limit is the real brake.
        per_ip_limit: int = 100,
        per_email_limit: int = 5,
        window_seconds: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._by_ip = SlidingWindowCounter(per_ip_limit, window_seconds, clock)
        self._by_email = SlidingWindowCounter(per_email_limit, window_seconds, clock)

    def retry_after(self, ip: str, email: str) -> int:
        return max(self._by_ip.retry_after(ip), self._by_email.retry_after(email))

    def record_failure(self, ip: str, email: str) -> None:
        self._by_ip.record(ip)
        self._by_email.record(email)

    def record_success(self, email: str) -> None:
        self._by_email.clear(email)
