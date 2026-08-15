"""The supervisor that runs the fleet.

Locally this is an asyncio loop; on Cloud Run the same agents become Jobs
(Watcher, Auditor) and Pub/Sub push subscribers (Analyst, Dispatcher). The agent
code does not change between the two -- only which bus adapter is underneath.

`run_simulated()` is what makes a demo possible. With a FrozenClock, sleeping
advances time instead of spending it, so a week of road time passes in about a
second and the Auditor's twenty-six-hour escalation actually happens rather than
being something you have to take on faith.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from road_cleaner.agents.analyst import Analyst
from road_cleaner.agents.auditor import Auditor
from road_cleaner.agents.dispatcher import Dispatcher
from road_cleaner.agents.watcher import Watcher
from road_cleaner.domain.enums import EventTopic
from road_cleaner.logging import get_logger

log = get_logger(__name__)


@dataclass
class RunStats:
    ticks: int = 0
    polls: int = 0
    frames_skipped: int = 0
    frames_published: int = 0
    camera_failures: int = 0
    prefilter_kills: int = 0
    frames_analyzed: int = 0
    detections: int = 0
    cases_filed: int = 0
    unresolved: int = 0
    audits: int = 0
    cleared: int = 0
    escalated: int = 0
    flagged_for_human: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def skip_rate(self) -> float:
        return self.frames_skipped / self.polls if self.polls else 0.0

    @property
    def prefilter_rate(self) -> float:
        total = self.prefilter_kills + self.frames_analyzed
        return self.prefilter_kills / total if total else 0.0


class PipelineRunner:
    def __init__(self, container) -> None:
        self.c = container
        self.watcher = Watcher(container)
        self.analyst = Analyst(container)
        self.dispatcher = Dispatcher(container)
        self.auditor = Auditor(container, self.dispatcher)
        self._wired = False

    async def wire(self) -> None:
        """Connect agents to the bus. One place, both transports."""
        if self._wired:
            return
        await self.c.bus.subscribe(EventTopic.FRAME_CAPTURED, self.analyst.handle_frame)
        await self.c.bus.subscribe(EventTopic.HAZARD_CONFIRMED, self.dispatcher.handle_hazard)
        self._wired = True

    async def seed(self) -> int:
        return await self.watcher.seed_registry()

    async def run_simulated(
        self,
        *,
        minutes: int,
        step_seconds: int = 60,
        audit_every_minutes: int = 30,
        progress=None,
    ) -> RunStats:
        """Run the whole loop over simulated time.

        Each step advances the clock, polls whatever is due, drains the bus so
        every consequence of those frames plays out, and periodically runs the
        Auditor.
        """
        await self.wire()
        stats = RunStats()
        steps = max(1, (minutes * 60) // step_seconds)
        audit_every_steps = max(1, (audit_every_minutes * 60) // step_seconds)

        for step in range(steps):
            await self.watcher.tick()
            # Let the Analyst and Dispatcher fully catch up before time moves on,
            # so causality in the trail matches the clock.
            await self.c.bus.drain()

            if step % audit_every_steps == 0 and step > 0:
                stats.audits += await self.auditor.tick()
                await self.c.bus.drain()

            await self.c.clock.sleep(step_seconds)
            stats.ticks += 1
            if progress is not None:
                progress(step + 1, steps)

        # One final audit so cases that cleared at the very end are closed.
        stats.audits += await self.auditor.tick()
        await self.c.bus.drain()

        return await self._collect(stats)

    async def run_forever(self, *, step_seconds: int = 30) -> None:
        """Production loop. Runs until cancelled."""
        await self.wire()
        ticks = 0
        audit_every = max(1, self.c.settings.auditor_interval_seconds // step_seconds)
        while True:
            try:
                await self.watcher.tick()
                if ticks % audit_every == 0 and ticks > 0:
                    await self.auditor.tick()
            except Exception:  # noqa: BLE001 - a bad tick must not end the run
                log.exception("Tick failed; continuing")
            ticks += 1
            await self.c.clock.sleep(step_seconds)

    async def _collect(self, stats: RunStats) -> RunStats:
        stats.polls = self.watcher.polls
        stats.frames_skipped = self.watcher.skipped
        stats.frames_published = self.watcher.published
        stats.camera_failures = self.watcher.failures
        stats.prefilter_kills = self.analyst.prefilter_kills
        stats.frames_analyzed = self.analyst.analyzed
        stats.detections = self.analyst.detections
        stats.cases_filed = self.dispatcher.filed
        stats.unresolved = self.dispatcher.unresolved
        stats.cleared = self.auditor.cleared
        stats.escalated = self.auditor.escalated
        stats.flagged_for_human = self.auditor.flagged_for_human

        for case in await self.c.repository.list_cases(limit=10_000):
            key = case.kind.value
            stats.by_kind[key] = stats.by_kind.get(key, 0) + 1
        return stats
