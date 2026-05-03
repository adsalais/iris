from __future__ import annotations

import time
from collections import defaultdict


class TokenBucket:
    """Simple in-process token-bucket rate limiter.

    Each `key` maintains its own bucket. `take(key)` returns None if the
    request is allowed (and consumes one token), else returns the number of
    seconds the caller should wait before retrying.

    Designed for `--workers 1` in-memory deployments. Per-process state.
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        # _buckets[key] = (tokens, last_refill_monotonic)
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (capacity, time.monotonic())
        )

    def take(self, key: str) -> float | None:
        """Returns None if allowed, else seconds to wait until a token is available."""
        now = time.monotonic()
        tokens, last = self._buckets[key]
        # Refill since last
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return None
        # Not enough tokens; persist the refill so the next call sees it
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self.refill_per_second
