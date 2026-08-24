"""Run the whole agent against a hazard you describe, and stop before sending.

You cannot wait for a real shed truck tyre to appear on a public camera while a
judge is watching. A drill gives the fleet something to find: you type

    "a mattress in the fast lane on I-85 at rush hour"

and it invents a plausible location, produces footage of it, and then runs the
**real** pipeline over that footage — the same vision call, the same confidence
gate, the same jurisdiction rules, the same report composition the system uses
on a genuine detection. It stops at a finished report and refuses to send it.

What is invented and what is real, precisely:

* invented — the location, the camera, and the imagery
* real — both vision calls, the gate's arithmetic, the agency lookup, the
  report text, and the decision about whether this would have been filed at all

The corroboration is worth being careful about. The gate exists to require two
independent observations of the same hazard, 90 seconds to 30 minutes apart,
before a case can be opened. A drill satisfies that honestly: two frames are
produced and **both are sent to the vision model separately**. Only the clock is
invented. Fabricating a second detection row to satisfy the gate would defeat
the one check the entire system is built around, so it is not done.

Nothing here can reach an agency. The drill calls `compose()`, which the filing
channels guarantee is side-effect free, and never `transmit()`; the case is
marked `synthetic`, which `Dispatcher._file_locked` refuses outright.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from road_cleaner.adapters.camera.scene import SceneSpec, phash, render
from road_cleaner.domain import narrative
from road_cleaner.domain.enums import CaseKind, HazardType, Severity, Stage, Tone
from road_cleaner.domain.gating import evaluate
from road_cleaner.domain.models import (
    Camera,
    Case,
    Detection,
    Filing,
    Frame,
    FrameRef,
    TrailEvent,
)
from road_cleaner.logging import get_logger
from road_cleaner.ports.media import SYNTHETIC_PREFIX

log = get_logger(__name__)

# The six stages a drill goes through, in order. `push` is deliberately absent:
# a drill has no seventh stage, and showing one greyed out would suggest the
# system merely ran out of time rather than declined on purpose.
STAGES: list[tuple[str, str]] = [
    ("scaffold", "Scaffold"),
    ("stage", "Stage"),
    ("detect", "Detect"),
    ("confirm", "Confirm"),
    ("resolve", "Resolve"),
    ("report", "Report"),
]

# The two observations are stamped this far apart on the invented timeline. The
# gate wants 90s-1800s; four minutes sits comfortably inside that and reads as a
# plausible gap between two camera polls.
OBSERVATION_GAP = timedelta(minutes=4)

_SIM_PREFIX = "SIM"


class DrillError(RuntimeError):
    """A drill could not be completed. The message is shown to the user."""


@dataclass
class StageReport:
    key: str
    label: str
    state: str = "pending"  # pending | running | done | failed | blocked
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "state": self.state, "detail": self.detail}


@dataclass
class DrillResult:
    """Everything the console needs to render one run."""

    stages: list[StageReport]
    case_id: str | None = None
    prompt: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    frame_urls: list[str] = field(default_factory=list)
    clip_url: str | None = None
    detections: list[dict[str, Any]] = field(default_factory=list)
    gate_decision: str | None = None
    gate_reason: str | None = None
    agency: str | None = None
    agency_rule: str | None = None
    agency_rationale: str | None = None
    report_subject: str | None = None
    report_body: str | None = None
    filed: bool = False  # always False. Present so the UI can assert it.

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "stages"}
        data["stages"] = [s.as_dict() for s in self.stages]
        return data


# --------------------------------------------------------------- the scaffold

_SCAFFOLD_PROMPT = """You turn a short description of a road hazard into a structured record.

Reply with ONLY a JSON object, no prose and no code fence, with exactly these keys:
  "state"        one of GA, FL, NC
  "road"         a road designation like "I-85", "I-4", "US-70", "GA-400"
  "direction"    "northbound", "southbound", "eastbound" or "westbound"
  "county"       a real county in that state
  "place"        a short landmark or interchange name, lowercase
  "hazard_type"  one of: debris, stalled_vehicle, unreported_closure, flooding,
                 infrastructure_damage, animal
  "lane_position" one of: lane_1, lane_2, lane_3, left_shoulder, right_shoulder
  "severity"     one of: low, medium, high, critical
  "description"  one factual sentence describing what is on the road, as a
                 traffic camera would see it. No drama, no speculation.

If the description does not mention a state or road, choose plausible ones in
the Southeast US. Never choose a hazard involving a person.

Description: {text}"""


def _parse_json(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model response.

    Gemma fences its JSON in markdown regardless of instructions, so strip that
    before giving up. Same problem and same fix as `gemini_vision._parse_json`.
    """
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


# Where each state's invented cameras sit, roughly. Real coordinates matter only
# because the gate checks whether the state's own feed already has an event
# within 500 m, and that check should run against a plausible location.
_STATE_CENTRES = {
    "GA": (33.78, -84.42),
    "FL": (28.54, -81.38),
    "NC": (35.78, -78.64),
}


class Drill:
    """One drill run against a container.

    Holds no state between runs: every call to `run` produces its own case.
    """

    def __init__(self, container, *, scaffold_model: str | None = None) -> None:
        self.c = container
        self.settings = container.settings
        # Gemma writes the scaffold. It is a small, cheap, fast model and this is
        # a small, cheap, fast job -- turning one sentence into six fields. It is
        # deliberately kept off the detection path, where accuracy matters more
        # than latency and Gemini earns its cost.
        self.scaffold_model = scaffold_model or self.settings.gemma_model
        self._genai_client = None

    # ------------------------------------------------------------- plumbing
    def _client(self):
        if self._genai_client is not None:
            return self._genai_client
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise DrillError(
                "google-genai is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc
        s = self.settings
        if not s.google_cloud_project:
            raise DrillError("GOOGLE_CLOUD_PROJECT must be set to run a drill.")
        self._genai_client = genai.Client(
            vertexai=True, project=s.google_cloud_project, location=s.google_cloud_location
        )
        return self._genai_client

    async def _scaffold(self, text: str) -> dict[str, Any]:
        client = self._client()
        try:
            response = await client.aio.models.generate_content(
                model=self.scaffold_model,
                contents=_SCAFFOLD_PROMPT.format(text=text.strip()),
            )
        except Exception as exc:  # noqa: BLE001
            raise DrillError(f"Scaffolding failed: {exc}") from exc

        spec = _parse_json(response.text or "")
        if spec is None:
            raise DrillError(f"Could not read the scaffold: {(response.text or '')[:160]}")

        try:
            spec["hazard_type"] = HazardType(spec["hazard_type"]).value
            spec["severity"] = Severity(spec.get("severity", "medium")).value
        except (KeyError, ValueError) as exc:
            raise DrillError(f"Scaffold named something we do not model: {exc}") from exc
        if spec["state"] not in _STATE_CENTRES:
            raise DrillError(f"Unsupported state: {spec['state']!r}. This fleet covers GA, FL, NC.")
        return spec

    @staticmethod
    def _place(raw: Any) -> str:
        """Tidy the landmark Gemma returned.

        `narrative.location_text` renders "I-75 northbound at {name}", so a name
        that already begins with "at" produces "at at i-285 interchange". Cheaper
        to strip it here than to keep tightening the prompt against a model that
        will occasionally do it anyway.
        """
        place = " ".join(str(raw or "").split()).strip(" ,.")
        for prefix in ("at the ", "at "):
            if place.lower().startswith(prefix):
                place = place[len(prefix):]
                break
        return place or "unnamed interchange"

    def _camera(self, spec: dict[str, Any], case_id: str) -> Camera:
        lat, lng = _STATE_CENTRES[spec["state"]]
        return Camera(
            id=f"{_SIM_PREFIX}-CCTV-{case_id.split('-')[-1]}",
            state=spec["state"],
            name=self._place(spec.get("place")),
            road=spec["road"],
            direction=spec.get("direction"),
            lat=lat,
            lng=lng,
            county=spec.get("county"),
            snapshot_url="drill://invented",
            # Left unset on purpose. A camera that does not exist has no known
            # owner, so the jurisdiction rules cannot shortcut to `use_camera_owner`
            # and the ADK reasoner has to work the answer out from the road, the
            # county and the agency list. That is the case ADK was wired in for.
            owner_agency_id=None,
        )

    # ------------------------------------------------------------- the run
    async def run(self, text: str, *, full: bool = False, on_progress=None) -> DrillResult:
        """Drive the pipeline end to end. `full` adds a Veo render.

        `on_progress(result)` is called after each stage so a UI can stream.
        """
        stages = [StageReport(k, label) for k, label in STAGES]
        result = DrillResult(stages=stages, prompt=text.strip())
        by_key = {s.key: s for s in stages}

        async def advance(key: str, detail: str = "") -> None:
            by_key[key].state = "done"
            if detail:
                by_key[key].detail = detail
            if on_progress:
                await on_progress(result)

        async def begin(key: str) -> None:
            by_key[key].state = "running"
            if on_progress:
                await on_progress(result)

        if not text.strip():
            raise DrillError("Describe a hazard first.")

        repo = self.c.repository
        clock = self.c.clock

        # --- 1. scaffold -------------------------------------------------
        await begin("scaffold")
        spec = await self._scaffold(text)
        result.spec = spec
        case_id = await repo.next_case_id(_SIM_PREFIX)
        camera = self._camera(spec, case_id)
        await repo.upsert_camera(camera)
        await advance(
            "scaffold",
            f"{spec['hazard_type']} · {spec['road']} {spec.get('direction','')} · "
            f"{spec.get('county','?')} County, {spec['state']}",
        )

        # --- 2. stage the scene -----------------------------------------
        await begin("stage")
        hazard = HazardType(spec["hazard_type"])
        first_seen = clock.now()
        frames: list[tuple[Frame, bytes]] = []
        for index, moment in enumerate((first_seen, first_seen + OBSERVATION_GAP)):
            image, _ = render(
                SceneSpec(
                    camera_id=camera.id,
                    label=f"{camera.id}  {camera.road} {camera.direction or ''}".strip(),
                    timestamp_text=moment.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    hazard=hazard,
                    hazard_lane=spec.get("lane_position", "lane_2"),
                    # Different seeds so the two frames are genuinely different
                    # images, not the same bytes analysed twice.
                    seed=abs(hash(case_id)) % 10_000 + index,
                )
            )
            # Under SYNTHETIC_PREFIX because that is what `/media` will serve and
            # what `is_synthetic_key` recognises. Not in the evidence store: a
            # staged frame is not something a camera saw.
            key = f"{SYNTHETIC_PREFIX}{case_id}/{moment.strftime('%Y%m%dT%H%M%S')}.jpg"
            await self.c.media_blobs.put(key, image, content_type="image/jpeg")
            frame = Frame(
                camera_id=camera.id, captured_at=moment, blob_key=key, phash=phash(image),
                width=640, height=360,
            )
            await repo.save_frame(frame)
            frames.append((frame, image))
            result.frame_urls.append(f"/media/{key}")
        await advance("stage", f"2 frames, {int(OBSERVATION_GAP.total_seconds() // 60)} min apart")

        if full:
            await self._render_clip(result, spec, camera, case_id)

        # --- 3. detect ---------------------------------------------------
        await begin("detect")
        detections: list[Detection] = []
        for frame, image in frames:
            detection = await self.c.vision.analyze(image, frame, camera)
            if detection is None:
                raise DrillError(
                    "The vision model found no hazard in the staged frame. "
                    "Try describing the hazard more plainly."
                )
            await repo.save_detection(detection)
            detections.append(detection)
            result.detections.append(
                {
                    "hazard": detection.hazard_type.value,
                    "lane": detection.lane_position,
                    "confidence": round(detection.confidence, 2),
                    "description": detection.description,
                    "model": detection.model_name,
                    "at": frame.captured_at.strftime("%H:%M:%S"),
                }
            )
        await advance(
            "detect",
            f"{len(detections)} independent calls · "
            + " and ".join(f"{d.confidence:.2f}" for d in detections),
        )

        # --- 4. the real gate -------------------------------------------
        await begin("confirm")
        events = await self.c.cameras.list_events(camera.state)
        gate = evaluate(detections[-1], detections[:-1], events, camera, self.c.gate_config)
        result.gate_decision = gate.decision.value
        result.gate_reason = gate.reason
        await advance("confirm", f"{gate.decision.value} — {gate.reason}")

        case = await self._open_case(case_id, camera, detections, gate, frames)
        result.case_id = case.id

        # --- 5. jurisdiction (ADK) --------------------------------------
        await begin("resolve")
        verdict = await self.c.jurisdiction.resolve(camera, detections[-1], self.c.reasoner)
        if not verdict.resolved:
            by_key["resolve"].state = "failed"
            by_key["resolve"].detail = verdict.rationale or "No rule matched."
            by_key["report"].state = "blocked"
            by_key["report"].detail = "Held: nobody to send it to."
            if on_progress:
                await on_progress(result)
            return result
        result.agency = verdict.agency.name
        result.agency_rule = verdict.rule_id
        result.agency_rationale = verdict.rationale
        await advance("resolve", f"{verdict.agency.name}  ({verdict.rule_id})")

        # --- 6. compose, and stop ---------------------------------------
        await begin("report")
        subject, body = await self._compose(case, camera, detections, verdict.agency, first_seen)
        result.report_subject = subject
        result.report_body = body
        by_key["report"].state = "done"
        by_key["report"].detail = "Draft ready — not sent"
        if on_progress:
            await on_progress(result)

        await repo.append_trail(
            TrailEvent(
                case_id=case.id, stage=Stage.REPORT, tone=Tone.WARN,
                text=(
                    "Drill: report composed in full and deliberately not sent. The "
                    "location was invented, so there is no road to report."
                ),
            )
        )
        return result

    # ------------------------------------------------------------- helpers
    async def _render_clip(self, result, spec, camera, case_id) -> None:
        """Optional Veo render. Never blocks the pipeline if it fails."""
        from road_cleaner.adapters.media.scenario_prompt import scenario_prompt
        from road_cleaner.ports.media import MediaUnavailableError

        stub = Case(
            id=case_id, camera_id=camera.id, state=camera.state,
            hazard_type=HazardType(spec["hazard_type"]),
            hazard_title=str(spec.get("description") or spec["hazard_type"]),
            location=narrative.location_text(camera), synthetic=True,
        )
        try:
            clip = await self.c.video.render_scenario(
                prompt=scenario_prompt(stub, camera, spec.get("lane_position", "")),
                duration_seconds=8, case_id=case_id,
            )
        except MediaUnavailableError as exc:
            log.warning("Drill clip failed", extra={"case": case_id, "error": str(exc)})
            return
        result.clip_url = f"/media/{clip.key}"

    async def _open_case(self, case_id, camera, detections, gate, frames) -> Case:
        detection = detections[-1]
        case = Case(
            id=case_id,
            camera_id=camera.id,
            state=camera.state,
            kind=CaseKind.WATCHING,
            hazard_type=detection.hazard_type,
            hazard_title=narrative.hazard_title(detection),
            location=narrative.location_text(camera),
            severity=detection.severity,
            confidence=detection.confidence,
            gate_decision=gate.decision,
            gate_reason=gate.reason,
            sentence=narrative.sentence(detection, gate, camera),
            explain=narrative.explain(detection, gate, camera),
            detection_ids=[d.id for d in detections],
            frame_refs=[
                FrameRef(
                    label="First sighting" if i == 0 else "Confirmation",
                    captured_at=f.captured_at, blob_key=f.blob_key, mark=(i == len(frames) - 1),
                )
                for i, (f, _) in enumerate(frames)
            ],
            raw_model_json=detection.raw_model_json,
            box=detection.box,
            box_label=f"{detection.hazard_type.value} · {detection.confidence:.2f}",
            # The flag that keeps this out of the log, the statistics, the
            # Auditor's queue and the filing path.
            synthetic=True,
        )
        await self.c.repository.save_case(case)
        await self.c.repository.append_trail(
            TrailEvent(
                case_id=case.id, stage=Stage.CONFIRM, tone=Tone.ROUTINE,
                text=narrative.gate_trail_text(gate),
            )
        )
        return case

    async def _compose(self, case, camera, detections, agency, first_seen) -> tuple[str, str]:
        """Build the report exactly as the Dispatcher would -- then stop.

        `compose()` is the filing channels' side-effect-free half: it renders
        what would be sent without sending it. `transmit()` is never called, and
        could not succeed anyway -- the case is synthetic and the Dispatcher
        refuses those outright.
        """
        from road_cleaner.container import channel_for_agency

        detection = detections[-1]
        body = narrative.report_body(
            detection,
            case.location,
            narrative.observed_at(first_seen),
            # A drill encloses nothing: the `Filing` below carries no attachments
            # and could not be sent anyway.
            attachment_count=0,
            tier=1,
        )
        subject = narrative.report_subject(detection, camera.road, tier=1)

        filing = Filing(
            case_id=case.id, agency_id=agency.id, channel=agency.channel,
            tier=1, subject=subject, body=body,
        )
        try:
            channel = channel_for_agency(agency.channel, self.settings)
            composed = channel.compose(filing, case, agency)
        except Exception as exc:  # noqa: BLE001 - a preview must never be fatal
            log.warning("Compose preview failed", extra={"error": str(exc)})
            return subject, body
        return getattr(composed, "subject", subject), getattr(composed, "body", body)
