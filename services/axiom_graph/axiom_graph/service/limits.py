"""Request limits for the Axiom Graph API (P1): batch-size cap + per-user rate limit.

Both are DoS controls. Each dependency in a ``/codemods`` batch triggers an sdist
download + Griffe/SSA analysis (CPU-seconds), so an unbounded batch — or a request
flood from a single account — can starve the service even after the auth gate.

The rate limiter is in-process (per worker): good enough for a single instance;
a multi-worker/replica deploy should move it to a shared store (e.g. Redis). Both
limits are env-configurable.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque


def max_dependencies_per_request() -> int:
    """Cap on dependencies analyzed in one request (env MAX_DEPENDENCIES_PER_REQUEST)."""
    try:
        return max(1, int(os.environ.get("MAX_DEPENDENCIES_PER_REQUEST", "50")))
    except ValueError:
        return 50


def rate_limit_per_minute() -> int:
    """Per-user request budget per minute (env RATE_LIMIT_PER_MINUTE)."""
    try:
        return max(1, int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30")))
    except ValueError:
        return 30


class SlidingWindowLimiter:
    """Allow up to ``max_requests`` per ``window`` seconds per key (thread-safe, in-process)."""

    def __init__(self, max_requests: int, window: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Record a hit for ``key`` and return whether it is within budget."""
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        """Drop all recorded hits (test isolation / manual clear)."""
        with self._lock:
            self._hits.clear()


# Module-level limiter (per process). The budget is read once here; tests build
# their own instance or swap this one out.
limiter = SlidingWindowLimiter(rate_limit_per_minute())
