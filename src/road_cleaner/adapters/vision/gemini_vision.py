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

import json
import re
from pathlib import Path

from road_cleaner.domain.enums import HazardType, Severity
from road_cleaner.domain.models import BoundingBox, Camera, Detection, Frame
from road_cleaner.logging import get_logger
from road_cleaner.ports.vision import ClearanceCheck

log = get_logger(__name__)

PROMPTS = Path(__file__).resolve().parents[2] / "agents" / "prompts"

# One yes/no question. Phrased to bias toward "yes" because a false positive
# here costs a fraction of a cent and a false negative costs the hazard.
PREFILTER_PROMPT = (
    "This is a still from a traffic camera. Is there anything in it that is not "
    "ordinary moving traffic on a clear road -- any object, stopped vehicle, "
    "standing water, cones, animal or person on the carriageway?\n\n"
    "If you are at all unsure, answer YES. Answering NO discards this frame "
    "permanently, so only say NO when the road is plainly clear.\n\n"
    "Answer with exactly one word: YES or NO."
)


class VisionUnavailableError(RuntimeError):
    """The model could not be reached or gave an unusable answer.

    Raised rather than swallowed. A frame we failed to analyse is not a frame
    with no hazard in it.
    """


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
    ) -> None:
        self.model = model
        self.prefilter_model = prefilter_model
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
        client = self._get_client()
        try:
            response = await client.aio.models.generate_content(
                model=model, contents=contents
            )
        except Exception as exc:  # noqa: BLE001 - surface every failure as unavailable
            raise VisionUnavailableError(f"Gemini call failed: {exc}") from exc
        return (response.text or "").strip()

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
        return Detection(
            camera_id=camera.id,
            frame_id=frame.id,
            analyzed_at=frame.captured_at,
            hazard_type=hazard,
            lane_position=str(payload.get("lane_position", "unknown")),
            severity=severity,
            confidence=max(0.0, min(1.0, confidence)),
            description=str(payload.get("description", "")).strip(),
            visual_evidence=[str(x) for x in payload.get("visual_evidence", [])],
            box=_box_for(str(payload.get("lane_position", "unknown"))),
            raw_model_json=json.dumps(payload, indent=2),
            model_name=self.model,
        )

    # ---------------------------------------------------------- clearance
    async def verify_cleared(
        self, image: bytes, evidence_image: bytes, detection: Detection, camera: Camera
    ) -> ClearanceCheck:
        prompt = self.clearance_prompt.format(
            hazard_type=detection.hazard_type.value,
            lane_position=detection.lane_position,
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


def _box_for(lane: str) -> BoundingBox:
    """Approximate where in the frame a lane sits.

    Gemini is not asked for pixel coordinates -- models are unreliable at that,
    and a box drawn in the wrong place is worse than a box drawn roughly. This
    maps the lane it did report onto a sensible region, which is all the overlay
    needs to be useful.
    """
    centers = {
        "left_shoulder": 0.30, "lane_1": 0.39, "lane_2": 0.50, "lane_3": 0.61,
        "right_shoulder": 0.70, "median": 0.24, "median_barrier": 0.24,
        "intersection": 0.50, "all_lanes": 0.50,
    }
    center = centers.get(lane, 0.5)
    width = 0.4 if lane == "all_lanes" else 0.14
    return BoundingBox(x=max(0.0, center - width / 2), y=0.60, width=width, height=0.18)
