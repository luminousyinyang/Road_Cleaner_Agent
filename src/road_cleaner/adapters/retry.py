"""Backing off when Vertex says "not now".

Every adapter that calls a Google model needs the same three things and got them
in different places, or not at all:

* a ceiling on how many calls are in flight,
* exponential backoff with jitter on transient refusals,
* and a firm refusal to retry anything that will fail identically next time.

A full-speed run with none of this produced 165 consecutive
`429 RESOURCE_EXHAUSTED` and zero detections. The pipeline ran to completion and
wrote nothing, without failing loudly -- which is worse than crashing.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

BASE_SECONDS = 1.0
CAP_SECONDS = 16.0

# Vertex says "too fast" as 429 RESOURCE_EXHAUSTED and "try again" as 503.
# ADK wraps its own as `_ResourceExhaustedError`, sometimes with an empty
# message -- so the exception's *type name* has to be checked as well as its
# text, or the wrapper sails straight through as if it were permanent.
_MARKERS = (
    "429", "RESOURCE_EXHAUSTED", "ResourceExhausted",
    "503", "UNAVAILABLE", "504", "DEADLINE_EXCEEDED",
)


def is_transient(exc: BaseException) -> bool:
    """Whether the same request is worth making again in a moment."""
    haystack = f"{type(exc).__name__} {exc}"
    return any(marker in haystack for marker in _MARKERS)


async def with_retry(
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int = 6,
    slots: asyncio.Semaphore | None = None,
    on_giveup: Callable[[BaseException, int], Exception] | None = None,
) -> T:
    """Run `call`, retrying transient failures with jittered backoff.

    `slots`, when given, bounds concurrency. The sleep happens *outside* it, so
    a call that is waiting its turn is not holding a slot another could use.

    Jitter matters more than it looks: without it every worker throttled at the
    same instant retries at the same instant, and the second wave collides
    exactly like the first.
    """
    delay = BASE_SECONDS
    last: BaseException | None = None

    for attempt in range(attempts):
        try:
            if slots is not None:
                async with slots:
                    return await call()
            return await call()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised below either way
            last = exc
            if not is_transient(exc) or attempt == attempts - 1:
                break
        await asyncio.sleep(delay + random.uniform(0, delay * 0.25))
        delay = min(delay * 2, CAP_SECONDS)

    assert last is not None
    raise on_giveup(last, attempts) if on_giveup else last
