"""The ADK multi-agent team.

Exports a `root_agent`, which is what `adk web` and `adk run` look for -- so the
pipeline's reasoning can be driven interactively from the ADK dev UI, separately
from the background loop that normally calls it.

The orchestration pattern here is **sequential with specialists**: a coordinator
delegating to four sub-agents that mirror the real pipeline stages. That mirror
is the point. The production system does not route work by asking a model what
to do next -- a Cloud Scheduler tick, a Pub/Sub message and a confidence gate do
that, deterministically. What these agents own is the reasoning inside each
stage, and running them under a coordinator makes that reasoning inspectable in
one place.

Read `adapters/reasoning/adk_reasoner.py` for the line between the two.
"""

from __future__ import annotations

from road_cleaner.domain.enums import HazardType
from road_cleaner.domain.sla import DEFAULT_SLA_HOURS

WATCHER_INSTRUCTION = """
You explain and reason about camera scheduling for a road-monitoring fleet.

Polling tiers:
- busy corridors: every 2 minutes
- quiet rural cameras: every 10 minutes
- any camera with an open case: every minute

A frame identical to the previous one is skipped, but never more than ten times
in a row -- an average hash cannot see a hazard, so an unbroken run of skips
would eventually hide one.

You do not fetch anything yourself. You answer questions about what the fleet is
doing and why.
""".strip()

ANALYST_INSTRUCTION = """
You explain the hazard confirmation logic.

A detection becomes a case only if it survives, in order:
1. a confidence floor of 0.55
2. corroboration by a second frame 90s to 30min later
3. no active official incident within 500m that already describes it
4. a confidence bar that scales with severity:
   critical 0.60, high 0.70, medium 0.80, low 0.88

Between the bar and 0.15 below it, the case is watched rather than reported.

The bias is deliberate and one-directional: prefer missing a hazard to reporting
one that is not there.
""".strip()

DISPATCHER_INSTRUCTION = """
You explain how a confirmed hazard is routed to the responsible agency.

Rules run first: toll authorities own their own roads, cities own signals and
surface streets inside city limits even on state routes, and the camera's stated
owner is believed when present. Only genuinely ambiguous cases reach a model,
and if that cannot decide either, the case is held rather than misfiled.

Reports go out via Open311, a maintenance web form, or structured email --
whichever the agency accepts. Everything is dry-run by default.
""".strip()

AUDITOR_INSTRUCTION = f"""
You explain what happens after a report is filed.

Each hazard type has a window it is expected to be cleared in:
{chr(10).join(f"- {h.value}: {int(DEFAULT_SLA_HOURS[h])}h" for h in HazardType)}

The camera is re-checked on a decaying schedule, more often when a case is
overdue. If the hazard is gone, the case closes with a before/after pair from
the same camera. If the deadline passes it is filed again one tier up; if it
passes twice, filing stops and a human is flagged.

An agent that re-sends the same message forever is spam. Stopping is a feature.
""".strip()

COORDINATOR_INSTRUCTION = """
You are Road Cleaner: an autonomous agent fleet that reads road footage --
a dashcam pass, or a public traffic camera -- finds hazards nobody has reported,
works out which agency owns that stretch of road, files the report, and keeps
checking until the road is actually clear.

You coordinate four specialists:
- watcher: camera polling and scheduling
- analyst: detection and the confidence gate
- dispatcher: jurisdiction and filing
- auditor: re-verification and escalation

Route questions to whichever specialist owns that stage. Be concrete and honest
about the system's limits -- particularly that it is dry-run by default and
deliberately biased toward missing hazards rather than reporting false ones.
""".strip()


def build_root_agent(model: str):
    """Build the coordinator and its sub-agents.

    Imported lazily inside the function so this module can be imported (and the
    file read, and the instructions tested) without `google-adk` installed.
    """
    from google.adk.agents import LlmAgent

    specialists = [
        LlmAgent(
            name="watcher",
            model=model,
            description="Camera polling schedules and frame capture.",
            instruction=WATCHER_INSTRUCTION,
        ),
        LlmAgent(
            name="analyst",
            model=model,
            description="Hazard detection and the confidence gate.",
            instruction=ANALYST_INSTRUCTION,
        ),
        LlmAgent(
            name="dispatcher",
            model=model,
            description="Jurisdiction resolution and report filing.",
            instruction=DISPATCHER_INSTRUCTION,
        ),
        LlmAgent(
            name="auditor",
            model=model,
            description="Re-verification, clearance and escalation.",
            instruction=AUDITOR_INSTRUCTION,
        ),
    ]

    return LlmAgent(
        name="road_cleaner",
        model=model,
        description=(
            "Autonomous road hazard detection and dispatch across the Southeast."
        ),
        instruction=COORDINATOR_INSTRUCTION,
        sub_agents=specialists,
    )


def _default_model() -> str:
    from road_cleaner.config import get_settings

    return get_settings().gemini_model


class _LazyRootAgent:
    """Defers construction until something actually touches the agent.

    `adk web` imports this module to find `root_agent`, but so does the test
    suite and the local pipeline, neither of which has credentials. Building on
    attribute access keeps the import free.
    """

    def __init__(self) -> None:
        self._agent = None

    def _resolve(self):
        if self._agent is None:
            self._agent = build_root_agent(_default_model())
        return self._agent

    def __getattr__(self, item):
        return getattr(self._resolve(), item)


root_agent = _LazyRootAgent()
