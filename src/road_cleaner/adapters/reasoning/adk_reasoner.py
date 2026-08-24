"""Google ADK agents for the judgment calls.

Worth being explicit about where the line is drawn, because it is a deliberate
architectural choice rather than an omission.

ADK `LlmAgent`s are used for the two things in this system that are genuinely
matters of judgment:

* deciding which agency owns a stretch of road when the rules cannot
* wording a report a maintenance desk will actually read

Everything else -- when to poll, whether two frames corroborate, what the SLA
is, whether to escalate, whether to file at all -- stays in deterministic Python.
Those are control flow, and control flow that decides whether to contact a
government agency should be testable, reproducible, and incapable of being
talked into something by a well-phrased frame. The confidence gate is a pure
function for exactly this reason.

So: models judge, code decides. `ScriptedReasoner` implements the same interface
without a model, which is why the entire pipeline runs with no credentials.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field

from road_cleaner.adapters.retry import with_retry
from road_cleaner.domain.models import Agency, Camera, Detection
from road_cleaner.logging import get_logger
from road_cleaner.ports.reasoning import JurisdictionVerdict

log = get_logger(__name__)

APP_NAME = "road_cleaner"


class JurisdictionAnswer(BaseModel):
    """Structured output for the jurisdiction agent."""

    agency_id: str = Field(description="The id of the responsible agency, exactly as listed.")
    confidence: float = Field(description="0-1. Be honest; low is a valid answer.", ge=0, le=1)
    rationale: str = Field(description="One sentence explaining the choice.")


JURISDICTION_INSTRUCTION = """
You decide which public agency is responsible for a road hazard.

You are only asked when a rules engine could not decide, so these are the
genuinely ambiguous cases. Getting this wrong sends a report to a body that
cannot act on it, and the hazard stays on the road while everyone assumes it is
handled. A low-confidence answer is far more useful than a confident wrong one.

Principles that decide most cases:

- Toll roads and turnpikes run their own maintenance. They are never the DOT
  district the road passes through.
- Traffic signals, signs and surface streets inside a city usually belong to the
  city, even when the route carries a state or US highway number.
- Interstate and US route mainline pavement belongs to the state DOT district.
- If a camera's owning organisation is given, that is strong evidence.

Choose only from the candidate agencies given. Return the agency id exactly as
listed. If none of them is plausibly responsible, pick the closest and set
confidence below 0.4 so a human reviews it.
""".strip()

REPORT_INSTRUCTION = """
You improve the wording of a road hazard report that will be sent to a
government maintenance desk.

A correct draft already exists. Your job is to make it read well -- not to
change what it says.

Absolute rules:
- Never add, remove or alter a fact: no location, time, position, confidence,
  camera id or measurement may change.
- Never add detail that is not in the draft. You have not seen the image.
- Never make it sound more certain than the draft does.
- Keep it short and plain. This is read by a busy person, not a committee.
- Keep the closing note that it was filed automatically and can be replied to.

Return only the improved report text.
""".strip()


class AdkReasoner:
    """Reasoning backed by real ADK agents.

    Agents and the runner are built lazily, so importing this module costs
    nothing and a missing `google-adk` install produces a clear message rather
    than an import error at startup.
    """

    def __init__(
        self,
        *,
        model: str,
        project: str | None = None,
        location: str = "us-central1",
        use_vertex: bool = True,
        max_concurrency: int = 2,
        max_retries: int = 6,
    ) -> None:
        self.model = model
        self.project = project
        self.location = location
        self.use_vertex = use_vertex
        # Lower than the vision ceiling on purpose: an ADK turn is several model
        # calls behind one await, so each slot costs more quota than it looks.
        self._slots = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._runner = None
        self._session_service = None

    @property
    def name(self) -> str:
        return f"adk:{self.model}"

    # ------------------------------------------------------------- wiring
    def _build(self):
        if self._runner is not None:
            return self._runner

        try:
            from google.adk.agents import LlmAgent
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise RuntimeError(
                "google-adk is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc

        if self.use_vertex and not self.project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT must be set to run ADK agents on Vertex AI.")

        from road_cleaner.agents.coordinator import build_root_agent

        self._agents = {
            "jurisdiction": LlmAgent(
                name="jurisdiction_agent",
                model=self.model,
                description="Decides which agency owns a stretch of road.",
                instruction=JURISDICTION_INSTRUCTION,
                output_schema=JurisdictionAnswer,
                output_key="jurisdiction",
            ),
            "report": LlmAgent(
                name="report_agent",
                model=self.model,
                description="Polishes the wording of an outgoing hazard report.",
                instruction=REPORT_INSTRUCTION,
                output_key="report",
            ),
        }
        self._session_service = InMemorySessionService()
        self._runner = Runner(
            app_name=APP_NAME,
            agent=build_root_agent(self.model),
            session_service=self._session_service,
        )
        return self._runner

    async def _ask(self, agent, prompt: str) -> str:
        """Run one agent to completion and return its final text.

        Retried and rate-limited exactly like the vision path. ADK had no
        protection at all: once the vision adapter stopped monopolising the
        quota, the jurisdiction agent started taking the 429s instead, and a
        throttled jurisdiction call means a case is held rather than filed.
        """
        return await with_retry(
            lambda: self._ask_once(agent, prompt),
            attempts=self._max_retries,
            slots=self._slots,
        )

    async def _ask_once(self, agent, prompt: str) -> str:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME, agent=agent, session_service=session_service
        )
        session = await session_service.create_session(app_name=APP_NAME, user_id="agent")

        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        final = ""
        async for event in runner.run_async(
            user_id="agent", session_id=session.id, new_message=message
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        return final.strip()

    # ------------------------------------------------------- jurisdiction
    async def resolve_jurisdiction(
        self, camera: Camera, detection: Detection, candidates: list[Agency]
    ) -> JurisdictionVerdict | None:
        if not candidates:
            return None

        # Inside the try below, not before it. `_build` raises a bare
        # RuntimeError when google-adk is absent or GOOGLE_CLOUD_PROJECT is
        # unset, and nothing between here and the route catches it -- so a
        # machine without the cloud extras answered /api/where with a naked
        # HTTP 500 and the map just said "HTTP 500". An unbuildable reasoner is
        # an unusable answer like any other: fall back to the registry rules.
        listing = "\n".join(
            f"- id: {a.id}\n  name: {a.name}\n  level: {a.level.value}\n"
            f"  note: {a.jurisdiction_note or '—'}"
            for a in candidates
        )
        prompt = (
            # Position stays here and only here: this prompt *is* the routing
            # decision, and `intersection` is what sends a damaged signal head to
            # the city rather than the state DOT.
            f"Hazard: {detection.hazard_type.value} at {detection.lane_position}\n"
            f"Description: {detection.description}\n\n"
            f"Camera: {camera.id}\n"
            f"Road: {camera.road} {camera.direction or ''}\n"
            f"Location: {camera.name}\n"
            f"County: {camera.county or 'unknown'}\n"
            f"State: {camera.state}\n"
            f"Camera's stated owner: {camera.owner_agency_id or 'not given'}\n\n"
            f"Candidate agencies:\n{listing}"
        )

        try:
            self._build()
            raw = await self._ask(self._agents["jurisdiction"], prompt)
            payload = json.loads(raw)
            answer = JurisdictionAnswer(**payload)
        except Exception as exc:  # noqa: BLE001 - an unusable answer is no answer
            log.warning("Jurisdiction agent failed", extra={"error": str(exc)})
            return None

        if answer.agency_id not in {a.id for a in candidates}:
            log.warning("Agent named an agency outside the candidate list")
            return None

        return JurisdictionVerdict(
            agency_id=answer.agency_id,
            confidence=answer.confidence,
            rationale=answer.rationale,
        )

    # -------------------------------------------------------------- prose
    async def polish_report(self, draft: str, detection: Detection, camera: Camera) -> str:
        """Improve the draft, and fall back to it on any doubt.

        The draft is already correct, so anything that goes wrong here costs
        style and nothing else. A returned value that looks like the model
        ignored the brief is discarded.
        """
        try:
            # Inside the try for the same reason as `resolve_jurisdiction`: a
            # missing google-adk raised straight past the fallback this
            # docstring promises, turning a cosmetic step into a hard failure.
            self._build()
            improved = await self._ask(self._agents["report"], f"Draft report:\n\n{draft}")
        except Exception as exc:  # noqa: BLE001
            log.warning("Report agent failed; sending the draft", extra={"error": str(exc)})
            return draft

        if not improved or len(improved) < len(draft) * 0.5:
            return draft
        # The facts live in the draft; if the model dropped the location line it
        # has rewritten rather than polished.
        if "Location:" in draft and "Location:" not in improved:
            return draft
        return improved
