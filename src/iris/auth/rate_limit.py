from __future__ import annotations

import time
from collections import OrderedDict

# Bound on the number of distinct rate-limit buckets held in memory at once.
# At ~32 bytes per (tokens, last_refill) entry plus key overhead, 10K caps the
# bucket dict at well under 1 MB regardless of input pattern. An attacker
# spraying >10K unique keys evicts older buckets in LRU order; legitimate
# clients are kept hot by their own activity.
_MAX_BUCKETS = 10_000


class TokenBucket:
    """In-process token-bucket rate limiter with bounded memory.

    Each ``key`` maintains its own bucket. ``take(key)`` returns None if the
    request is allowed (and consumes one token), else returns the number of
    seconds the caller should wait before retrying.

    Eviction: the bucket dict is an ``OrderedDict`` capped at ``_MAX_BUCKETS``
    entries. Calling ``take(key)`` promotes ``key`` to most-recently-used.
    Inserting a new key when at capacity drops the LRU entry. An evicted key
    re-inserted later starts with a fresh full-capacity bucket — equivalent
    to "we forgot you, here are ``capacity`` fresh tokens." Acceptable at the
    operational scale where the rate limiter alone cannot defend; a real
    DDoS demands an upstream WAF.

    Designed for ``--workers 1`` in-memory deployments. Per-process state.
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        # _buckets[key] = (tokens, last_refill_monotonic). Ordered for LRU.
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def take(self, key: str) -> float | None:
        """Returns None if allowed, else seconds to wait until a token is available."""
        now = time.monotonic()
        if key in self._buckets:
            tokens, last = self._buckets[key]
            self._buckets.move_to_end(key)
        else:
            tokens, last = (self.capacity, now)
            self._buckets[key] = (tokens, last)
            if len(self._buckets) > _MAX_BUCKETS:
                self._buckets.popitem(last=False)
        # Refill since last
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return None
        # Not enough tokens; persist the refill so the next call sees it.
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self.refill_per_second
