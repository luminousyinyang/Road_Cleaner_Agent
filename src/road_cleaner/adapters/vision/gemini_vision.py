"""Gemini on Vertex AI.

The expensive, accurate half of the vision pipeline. Two things matter here
beyond calling the API:

1. **The prefilter has to have high recall.** It exists to save money on empty
   roads, so when it is uncertain it must pass the frame on. A cheap model that
   discards a real hazard has cost far more than it saved -- nothing downstream
   can recover a frame that was never analysed.

2. **A model failure must never look like "no hazard".** If the API errors or
   returns something unparseable, this raises rather than returning None, so the
   frame is retried instead of being silently treated as clear road.

The client library is imported lazily so a local install without
`google-genai` still works.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from road_cleaner.adapters.retry import with_retry
from road_cleaner.domain.enums import HazardType, Severity
from road_cleaner.domain.models import BoundingBox, Camera, Detection, Frame
from road_cleaner.logging import get_logger
from road_cleaner.ports.vision import ClearanceCheck, VisionUnavailableError

log = get_logger(__name__)

PROMPTS = Path(__file__).resolve().parents[2] / "agents" / "prompts"

# One yes/no question. Phrased to bias toward "yes" because a false positive
# here costs a fraction of a cent and a false negative costs the hazard.
PREFILTER_PROMPT = (
    "This is a still of a road. Is there anything in it that is not "
    "ordinary moving traffic on a clear road -- any object, stopped vehicle, "
    "standing water, cones, animal or person on the carriageway?\n\n"
    "If you are at all unsure, answer YES. Answering NO discards this frame "
    "permanently, so only say NO when the road is plainly clear.\n\n"
    "Answer with exactly one word: YES or NO."
)


class GeminiVisionAnalyzer:
    def __init__(
        self,
        *,
        model: str,
        project: str | None = None,
        location: str = "us-central1",
        use_vertex: bool = True,
        api_key: str | None = None,
        prefilter_model: str | None = None,
        max_concurrency: int = 4,
        max_retries: int = 5,
    ) -> None:
        self.model = model
        self.prefilter_model = prefilter_model
        self.max_retries = max_retries
        # Vertex answers 429 RESOURCE_EXHAUSTED long before this pipeline runs
        # out of frames to send. The Analyst handles every `frame.captured` event
        # as it arrives, so without a ceiling a busy tick fires hundreds of
        # concurrent vision calls and Vertex refuses nearly all of them -- a
        # measured run produced 165 consecutive 429s and zero detections.
        self._slots = asyncio.Semaphore(max_concurrency)
        self._client = None
        self._config = {
            "project": project,
            "location": location,
            "use_vertex": use_vertex,
            "api_key": api_key,
        }
        self.analyze_prompt = (PROMPTS / "analyst.md").read_text()
        self.clearance_prompt = (PROMPTS / "clearance.md").read_text()

    @property
    def model_name(self) -> str:
        return self.model

    def _get_client(self):
        """Build the genai client on first use."""
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise VisionUnavailableError(
                "google-genai is not installed. Install it with:\n"
                "    uv pip install -e '.[cloud]'"
            ) from exc

        cfg = self._config
        if cfg["use_vertex"]:
            if not cfg["project"]:
                raise VisionUnavailableError(
                    "GOOGLE_CLOUD_PROJECT must be set to use Gemini via Vertex AI."
                )
            self._client = genai.Client(
                vertexai=True, project=cfg["project"], location=cfg["location"]
            )
        else:
            if not cfg["api_key"]:
                raise VisionUnavailableError(
                    "GOOGLE_API_KEY must be set to use the Gemini Developer API."
                )
            self._client = genai.Client(api_key=cfg["api_key"])
        return self._client

    @staticmethod
    def _part(image: bytes):
        from google.genai import types

        return types.Part.from_bytes(data=image, mime_type="image/jpeg")

    async def _generate(self, model: str, contents: list) -> str:
        """One model call, rate-limited and retried. See `adapters.retry`."""
        client = self._get_client()

        async def call() -> str:
            response = await client.aio.models.generate_content(
                model=model, contents=contents
            )
            return (response.text or "").strip()

        return await with_retry(
            call,
            attempts=self.max_retries + 1,
            slots=self._slots,
            on_giveup=lambda exc, n: VisionUnavailableError(
                f"Gemini call failed after {n} attempts: {exc}"
            ),
        )

    # ---------------------------------------------------------- prefilter
    async def prefilter(self, image: bytes, frame: Frame, camera: Camera) -> bool:
        """Cheap anomaly check, ideally on a small Gemma model.

        Fails open. If the prefilter is broken or unreachable we pay for the
        full analysis rather than dropping frames we never actually looked at.
        """
        if not self.prefilter_model:
            return True
        try:
            answer = await self._generate(
                self.prefilter_model, [PREFILTER_PROMPT, self._part(image)]
            )
        except VisionUnavailableError as exc:
            log.warning(
                "Prefilter unavailable; passing the frame through",
                extra={"error": str(exc)},
            )
            return True
        return not answer.upper().startswith("NO")

    # ------------------------------------------------------------ analyze
    async def analyze(self, image: bytes, frame: Frame, camera: Camera) -> Detection | None:
        context = (
            f"\n\nCamera context: {camera.id} on {camera.road} "
            f"{camera.direction or ''} at {camera.name}, {camera.state}."
        )
        raw = await self._generate(
            self.model, [self.analyze_prompt + context, self._part(image)]
        )
        payload = _parse_json(raw)
        if payload is None:
            raise VisionUnavailableError(f"Model returned unparseable output: {raw[:200]}")

        if not payload.get("hazard_present"):
            return None

        try:
            hazard = HazardType(payload["hazard_type"])
            severity = Severity(payload.get("severity", "medium"))
        except (KeyError, ValueError) as exc:
            raise VisionUnavailableError(f"Model returned an unknown hazard: {exc}") from exc

        confidence = float(payload.get("confidence", 0.0))
        # A box the model drew is evidence. There is no longer a second kind:
        # the fallback used to invent one from the lane name, and the lane name
        # is gone. No box is a truthful answer; a box in the middle of the frame
        # because we had nothing better is not.
        measured = _box_from(payload)
        return Detection(
            camera_id=camera.id,
            frame_id=frame.id,
            analyzed_at=frame.captured_at,
            hazard_type=hazard,
            lane_position=_position(payload),
            severity=severity,
            confidence=max(0.0, min(1.0, confidence)),
            description=str(payload.get("description", "")).strip(),
            visual_evidence=[str(x) for x in payload.get("visual_evidence", [])],
            box=measured,
            box_is_measured=measured is not None,
            raw_model_json=json.dumps(payload, indent=2),
            model_name=self.model,
        )

    # ---------------------------------------------------------- clearance
    async def verify_cleared(
        self, image: bytes, evidence_image: bytes, detection: Detection, camera: Camera
    ) -> ClearanceCheck:
        prompt = _fill(
            self.clearance_prompt,
            hazard_type=detection.hazard_type.value,
            position=detection.lane_position,
            description=detection.description,
        )
        raw = await self._generate(
            self.model,
            [
                prompt,
                "Evidence frame, captured when the hazard was reported:",
                self._part(evidence_image),
                "Current frame:",
                self._part(image),
            ],
        )
        payload = _parse_json(raw)
        if payload is None:
            # Unreadable answer means we do not know. Assume it is still there:
            # closing a case wrongly stops a real hazard being watched.
            log.warning("Unparseable clearance answer; keeping the case open")
            return ClearanceCheck(True, 0.0, "Could not read the model's answer.")

        return ClearanceCheck(
            still_present=bool(payload.get("still_present", True)),
            confidence=float(payload.get("confidence", 0.0)),
            note=str(payload.get("note", "")),
        )


def _fill(template: str, **values: str) -> str:
    """Substitute {placeholders} without treating the rest of the text as format syntax.

    `str.format` cannot be used here. The clearance prompt ends with a JSON
    example, and `{"still_present": ...}` is a perfectly good format field name
    as far as `format` is concerned -- it raised

        KeyError: '\n  "still_present"'

    on every single clearance check. Which meant the Auditor's "is it still
    there?" call, the thing this whole system is built around, had never once
    run against a real model: the scripted analyzer never touches this method,
    so the local test suite was green throughout.

    Explicit replacement leaves every other brace in the file alone.
    """
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def _parse_json(text: str) -> dict | None:
    """Pull a JSON object out of a model response.

    Models fence their JSON in markdown more often than not, whatever the prompt
    says, so strip that before giving up.
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


def _box_from(payload: dict) -> BoundingBox | None:
    """The box the model actually drew, if it drew one.

    `box_2d` arrives as [ymin, xmin, ymax, xmax] normalised 0-1000, which is the
    convention Gemini uses for spatial grounding. `BoundingBox` stores fractions
    of the frame, so this is a divide and a reorder.

    Returns None rather than guessing when the model omitted the box or returned
    something unusable -- the caller falls back, and a fallback that announces
    itself is better than a plausible-looking wrong answer.
    """
    raw = payload.get("box_2d")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (float(v) / 1000.0 for v in raw)
    except (TypeError, ValueError):
        return None

    # Models occasionally emit the corners the other way round.
    if xmax < xmin:
        xmin, xmax = xmax, xmin
    if ymax < ymin:
        ymin, ymax = ymax, ymin

    x = max(0.0, min(1.0, xmin))
    y = max(0.0, min(1.0, ymin))
    width = max(0.0, min(1.0 - x, xmax - xmin))
    height = max(0.0, min(1.0 - y, ymax - ymin))
    if width <= 0.0 or height <= 0.0:
        return None
    return BoundingBox(x=x, y=y, width=width, height=height)


# What the model may say about where the hazard sits. Deliberately short, and
# deliberately free of lane numbers: from a camera pointed down a road you cannot
# count the lanes to your left, so a lane number is a guess wearing the clothes of
# an observation. It was wrong often enough to reach case titles and the Location
# line of reports addressed to a DOT.
#
# What survives is what a single frame can actually establish, and `intersection`
# earns its place by routing a damaged signal head to the city rather than the
# state -- see the `municipal-signal` rule in seeds/agencies.yaml.
POSITIONS = frozenset(
    {"intersection", "left_shoulder", "right_shoulder", "median", "median_barrier"}
)


def _position(payload: dict) -> str:
    """Where the model says it is, or `unknown` if that is not something we take.

    An old prompt, a cached response or a model reaching for its training data can
    all still say `lane_2`. Anything outside `POSITIONS` becomes `unknown` rather
    than being stored -- the whole point is that these values feed jurisdiction
    routing, and a value we do not trust must not be able to pick an agency.
    """
    raw = payload.get("position", payload.get("lane_position", ""))
    value = str(raw).strip().lower()
    return value if value in POSITIONS else "unknown"

