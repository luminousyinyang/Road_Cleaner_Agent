"""The event bus between agents.

The four agents never call each other. The Watcher publishes a frame and stops
caring; the Analyst picks it up whenever it can. That decoupling is what lets
the same agent code run as an asyncio task locally and as a Pub/Sub push
subscriber on Cloud Run, and it is what stops one slow camera from stalling
the fleet.

Publishing is the only thing agents do to the bus. Consumption is wired up once
in `pipeline/handlers.py`, so there is exactly one mapping from topic to handler
regardless of which transport is carrying the messages.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from road_cleaner.domain.enums import EventTopic

Handler = Callable[[dict], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, topic: EventTopic, payload: dict) -> None:
        """Fire and forget. Must not raise on a slow or absent consumer."""
        ...

    async def subscribe(self, topic: EventTopic, handler: Handler) -> None:
        """Register a handler. Local transports call it; Pub/Sub push does not
        (the HTTP route is the subscription there)."""
        ...

    async def start(self) -> None: ...

    async def drain(self) -> None:
        """Block until every queued message has been handled.

        Exists for tests and for `make demo`: it means a run can be driven to
        completion deterministically instead of by sleeping and hoping.
        """
        ...

    async def close(self) -> None: ...
