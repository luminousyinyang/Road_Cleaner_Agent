"""In-process event bus.

Same semantics as Pub/Sub -- publish is fire-and-forget, handlers run
independently, a failing handler doesn't take down the publisher -- but without
needing a project or credentials.

The one thing this has that Pub/Sub doesn't is `drain()`, which blocks until
every queued message has been fully processed, including messages produced *by*
handlers. That turns "run the pipeline and see what happens" into a
deterministic operation, which is what makes both the tests and `make demo`
reliable instead of sleep-and-hope.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from road_cleaner.domain.enums import EventTopic
from road_cleaner.logging import get_logger
from road_cleaner.ports.event_bus import Handler

log = get_logger(__name__)


class InMemoryEventBus:
    def __init__(self, *, max_queue: int = 10_000) -> None:
        self._handlers: dict[EventTopic, list[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue[tuple[EventTopic, dict]] = asyncio.Queue(maxsize=max_queue)
        self._workers: list[asyncio.Task] = []
        self._running = False
        self.dead_letters: list[tuple[EventTopic, dict, str]] = []

    async def publish(self, topic: EventTopic, payload: dict) -> None:
        await self._queue.put((topic, payload))

    async def subscribe(self, topic: EventTopic, handler: Handler) -> None:
        self._handlers[topic].append(handler)

    async def start(self, workers: int = 4) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(workers)]

    async def _worker(self, index: int) -> None:
        while self._running:
            try:
                topic, payload = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._dispatch(topic, payload)
            finally:
                self._queue.task_done()

    async def _dispatch(self, topic: EventTopic, payload: dict) -> None:
        for handler in self._handlers.get(topic, []):
            try:
                await handler(payload)
            except Exception as exc:  # noqa: BLE001 - a bad message must not kill the bus
                # The Pub/Sub equivalent of this is the dead-letter topic. One
                # malformed frame should cost us that frame, not the run.
                log.exception("Handler failed", extra={"topic": topic.value, "error": str(exc)})
                self.dead_letters.append((topic, payload, str(exc)))

    async def drain(self) -> None:
        """Wait until the queue is empty and all in-flight handlers have finished.

        Loops because handlers publish too -- the Analyst's output becomes the
        Dispatcher's input -- so the queue can refill while we're waiting on it.
        """
        while True:
            await self._queue.join()
            await asyncio.sleep(0)
            if self._queue.empty():
                return

    async def close(self) -> None:
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
