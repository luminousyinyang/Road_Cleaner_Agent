"""Pub/Sub event bus.

Publish-only, on purpose. In cloud mode the agents are Cloud Run services behind
**push** subscriptions, so consumption happens over HTTP — Pub/Sub POSTs an
envelope to a route, which hands the payload to the same handler the in-memory
bus would have called. `subscribe()` therefore just registers the handler in a
local table for that route to look up; there is no pull loop.

That is what keeps one set of agent code running under both transports.

Failures go to a dead-letter topic rather than being retried forever: a message
that cannot be processed twice will not be processed on the hundredth attempt,
and an infinite redelivery loop against a paid vision API is an expensive way to
find that out.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from road_cleaner.domain.enums import EventTopic
from road_cleaner.logging import get_logger
from road_cleaner.ports.event_bus import Handler

log = get_logger(__name__)


class PubSubEventBus:
    def __init__(
        self,
        project: str | None,
        topics: dict[str, str],
        dead_letter_topic: str | None = None,
    ) -> None:
        if not project:
            raise ValueError("GOOGLE_CLOUD_PROJECT must be set to use Pub/Sub")
        self.project = project
        self.topics = topics
        self.dead_letter_topic = dead_letter_topic
        self._publisher = None
        self._handlers: dict[EventTopic, list[Handler]] = defaultdict(list)

    def _get_publisher(self):
        if self._publisher is not None:
            return self._publisher
        try:
            from google.cloud import pubsub_v1
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise RuntimeError(
                "google-cloud-pubsub is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc
        self._publisher = pubsub_v1.PublisherClient()
        return self._publisher

    def _topic_path(self, topic: EventTopic) -> str:
        name = self.topics.get(topic.value, topic.value.replace(".", "-"))
        return self._get_publisher().topic_path(self.project, name)

    async def publish(self, topic: EventTopic, payload: dict) -> None:
        publisher = self._get_publisher()
        path = self._topic_path(topic)
        data = json.dumps(payload).encode()

        def send() -> None:
            publisher.publish(path, data).result(timeout=30)

        try:
            await asyncio.to_thread(send)
        except Exception as exc:  # noqa: BLE001 - publishing must not break a poll loop
            log.exception(
                "Publish failed", extra={"topic": topic.value, "error": str(exc)}
            )

    async def subscribe(self, topic: EventTopic, handler: Handler) -> None:
        """Register a handler for the push route to dispatch to.

        No pull subscriber is started: Pub/Sub delivers by POSTing to the Cloud
        Run service, and `dispatch()` is what that route calls.
        """
        self._handlers[topic].append(handler)

    async def dispatch(self, topic: EventTopic, payload: dict) -> None:
        """Run the handlers for a pushed message.

        Raises on failure so the HTTP route can return a non-2xx and let Pub/Sub
        redeliver — and, after the configured attempts, dead-letter it.
        """
        for handler in self._handlers.get(topic, []):
            await handler(payload)

    async def send_to_dead_letter(self, topic: EventTopic, payload: dict, error: str) -> None:
        if not self.dead_letter_topic:
            return
        publisher = self._get_publisher()
        path = publisher.topic_path(self.project, self.dead_letter_topic)
        body = json.dumps(
            {"original_topic": topic.value, "payload": payload, "error": error}
        ).encode()
        await asyncio.to_thread(lambda: publisher.publish(path, body).result(timeout=30))

    async def start(self) -> None:
        return None

    async def drain(self) -> None:
        """No-op. Pub/Sub has no local queue to drain."""
        return None

    async def close(self) -> None:
        return None
