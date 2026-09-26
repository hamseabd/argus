"""A token bucket."""

import time


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = float(capacity)
        self._updated = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False
