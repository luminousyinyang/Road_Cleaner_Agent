"""Time, as a dependency.

Almost everything interesting about this system is time-shaped: hazards have to
persist across frames to be believed, reports have deadlines, cases escalate
when nobody comes, and cameras get re-checked on a decaying schedule. If the
code reads the wall clock directly, none of that is testable and none of it is
demoable -- you would have to wait a real twenty-six hours to see an escalation.

So time comes through this port. `SystemClock` in production, `FrozenClock` in
tests and demos, where a whole week can pass in a millisecond.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, always timezone-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait. Virtual clocks advance instead of actually waiting."""
        ...


class SystemClock:
    """Real time. Used everywhere outside tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FrozenClock:
    """A clock you control.

    `sleep()` advances time instead of spending it, so a test can watch a case
    breach a twenty-four hour SLA without waiting, and `make demo` can generate
    a plausible week of history instantly.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 3, 14, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        # Yield so other tasks on the loop still get to run.
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

    def advance_hours(self, hours: float) -> datetime:
        return self.advance(hours * 3600)

    def set(self, moment: datetime) -> None:
        self._now = moment
