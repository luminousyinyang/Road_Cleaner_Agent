"""Client-side rate governor.

The state 511 platforms publish a limit of ten calls per sixty seconds per
developer key. Being throttled would be our fault, not theirs, and a project
that gets its key revoked has no product -- so the limit is enforced here rather
than discovered from a 429.

One bucket per state, because each state issues its own key and the limits are
independent.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    """A sliding-window limiter. Waits rather than failing.

    Deliberately conservative: the window is measured from when a call was
    *started*, and the limiter holds a lock across the wait so concurrent
    callers queue in order instead of all waking at once and bursting through.
    """

    def __init__(self, calls: int = 10, window_seconds: float = 60.0) -> None:
        self.calls = calls
        self.window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                cutoff = now - self.window
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.calls:
                    self._timestamps.append(now)
                    return

                # A small margin past the oldest call's expiry, so rounding
                # never puts us one call over.
                await asyncio.sleep(self._timestamps[0] - cutoff + 0.05)

    @property
    def in_window(self) -> int:
        cutoff = time.monotonic() - self.window
        return sum(1 for t in self._timestamps if t > cutoff)


class StateRateLimiters:
    """One limiter per state key."""

    def __init__(self, calls: int = 10, window_seconds: float = 60.0) -> None:
        self.calls = calls
        self.window_seconds = window_seconds
        self._limiters: dict[str, RateLimiter] = {}

    def for_state(self, state: str) -> RateLimiter:
        if state not in self._limiters:
            self._limiters[state] = RateLimiter(self.calls, self.window_seconds)
        return self._limiters[state]

    async def acquire(self, state: str) -> None:
        await self.for_state(state).acquire()
