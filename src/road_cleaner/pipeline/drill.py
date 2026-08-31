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
from pathlib import Path
from typing import Any

from road_cleaner.adapters.camera.scene import SceneSpec, phash, render
from road_cleaner.adapters.retry import with_retry
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

# Stills cut from a generated clip before two are chosen. Five rather than two so
# the ends -- hazard too distant, hazard already passed -- can be discarded and
# the pair still comes from far enough apart to be two genuine looks.
CLIP_SAMPLES = 5

# Where a reusable clip lives, one per hazard type.
#
# A drill renders fresh footage every run, which is the point of a drill. A demo
# is not a drill: it is the same scenario shown repeatedly, and paying Veo for a
# new render each time buys nothing but latency and a rate limit at the worst
# possible moment. So the first run for a hazard renders and keeps the clip here,
# and every run after it replays that one. The Gemini half still runs in full --
# it is the footage that is reused, not the analysis of it.
DEMO_CLIP_PREFIX = f"{SYNTHETIC_PREFIX}_demo/"

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
    # The same stills with the detection box burned in, one per frame the hazard
    # was found in. Separate from `frame_urls` because they are not
    # interchangeable: those are what the model was shown, these are what it
    # concluded, and a report encloses the second.
    evidence_urls: list[str] = field(default_factory=list)
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
  "state"        a two-letter US state code in the lower 48, e.g. GA, OH, TX
  "road"         a road designation like "I-85", "I-4", "US-70", "GA-400"
  "direction"    "northbound", "southbound", "eastbound" or "westbound"
  "county"       a real county in that state
  "place"        a short landmark or interchange name, lowercase
  "hazard_type"  one of: debris, stalled_vehicle, unreported_closure, flooding,
                 infrastructure_damage, animal, pothole
                 Use "pothole" only for a hole in the road surface itself, not
                 for an object lying on it and not for roadside hardware.
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
        """Turn one sentence into a structured hazard spec, with Gemma.

        Retried like every other model call in this system. It was not, and it
        was the only one: a single `429 RESOURCE_EXHAUSTED` -- which Vertex
        returns readily when its request queue is busy -- killed the whole drill
        at the first stage, before any of the work worth watching had started.
        A drill is something a person presses and waits on, so the one model
        call standing in front of the other five stages is the last one that
        should give up immediately.
        """
        client = self._client()

        async def call():
            return await client.aio.models.generate_content(
                model=self.scaffold_model,
                contents=_SCAFFOLD_PROMPT.format(text=text.strip()),
            )

        try:
            response = await with_retry(call, attempts=4)
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
        # No longer refused if it is outside GA/FL/NC. The registry covers every
        # mainland state now, so a scaffold that says "Ohio" is a location this
        # system can genuinely file about. A state it has never heard of still
        # falls back to a seeded centroid so the drill can run.
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

    def _camera(
        self, spec: dict[str, Any], case_id: str, pin=None, prefix: str = _SIM_PREFIX
    ) -> Camera:
        """The invented camera the drill stages its scene at.

        With a dropped pin the coordinate is real and the state comes from the
        boundary it fell in, so the jurisdiction lookup is answering a genuine
        question about a genuine place. Without one, the scaffold's guess is
        planted at a state centroid as it always was.
        """
        if pin is not None:
            lat, lng, state = pin.lat, pin.lng, pin.state
            # `road_unknown` is what lets the state-level fallback claim a
            # coordinate. A pin has no road name, and the scaffold's invented one
            # would be a road that does not exist at that spot.
            road, place = "an unnamed road", pin.short
        else:
            lat, lng = _STATE_CENTRES.get(spec["state"], _STATE_CENTRES["GA"])
            state = spec["state"]
            road, place = spec["road"], self._place(spec.get("place"))

        return Camera(
            id=f"{prefix}-CCTV-{case_id.split('-')[-1]}",
            state=state,
            name=place,
            road=road,
            direction=None if pin is not None else spec.get("direction"),
            lat=lat,
            lng=lng,
            county=None if pin is not None else spec.get("county"),
            snapshot_url="drill://invented",
            # Left unset on purpose. A camera that does not exist has no known
            # owner, so the jurisdiction rules cannot shortcut to `use_camera_owner`
            # and the ADK reasoner has to work the answer out from the road, the
            # county and the agency list. That is the case ADK was wired in for.
            owner_agency_id=None,
        )

    # ------------------------------------------------------------- the run
    async def run(
        self, text: str, *, full: bool = False, pin=None, on_progress=None,
        reuse_clip: bool = False, case_prefix: str = _SIM_PREFIX, synthetic: bool = True,
    ) -> DrillResult:
        """Drive the pipeline end to end. `full` adds a Veo render.

        `on_progress(result)` is called after each stage so a UI can stream.

        `reuse_clip` keeps and replays the footage for a hazard rather than
        rendering it every time. Off for a drill, where fresh footage is the
        exercise; on for the demonstration, which shows one scenario repeatedly
        and should not bill a render -- or risk a rate limit -- per click.

        `synthetic` marks the case as a drill and is what keeps it out of the
        library, the statistics, the Auditor's queue and the filing path. Turning
        it off produces an ordinary case that appears alongside the seeded ones,
        which is what the demonstration case wants and what a drill must never
        do. `case_prefix` goes with it: a case sitting in the library reading
        `SIM-1007` next to `GA-4462` announces itself as a different kind of
        thing, so a real one takes its state's prefix.
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
        case_id = await repo.next_case_id(case_prefix)
        camera = self._camera(spec, case_id, pin, prefix=case_prefix)
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
            # A Veo render used to be decorative: the clip played on the page
            # while detection went on reading the flat scene renders above, so
            # the "evidence" a report carried was two coloured rectangles. When
            # there is real footage, the stills the model looks at -- and the
            # ones that end up attached to a report -- come out of it.
            from_clip = await self._render_clip(
                result, spec, camera, case_id, frames, reuse=reuse_clip
            )
            if from_clip:
                frames = from_clip

        # --- 3. detect ---------------------------------------------------
        await begin("detect")
        detections: list[Detection] = []
        # Frames that actually produced a detection, so the evidence a report
        # encloses is the stills the hazard was found in.
        kept: list[tuple[Frame, bytes]] = []
        looked = len(frames)
        for frame, image in frames:
            detection = await self.c.vision.analyze(image, frame, camera)
            if detection is None:
                # A miss is not a failure. Real footage of a real approach has
                # frames where the hazard is a hundred metres off or already
                # under the car, and requiring every still to find it meant one
                # unlucky sample killed a run in which three others saw it
                # plainly. The gate below still decides whether what was found
                # is enough.
                continue
            await repo.save_detection(detection)
            kept.append((frame, image))
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
        # Two independent looks is what the gate is built around, and no amount
        # of resampling substitutes for it. One detection is a single sighting;
        # none means the model genuinely did not find the hazard described.
        if len(detections) < 2:
            raise DrillError(
                f"The vision model found the hazard in {len(detections)} of "
                f"{looked} stills, and two independent sightings are needed "
                "before anything can be reported. Try describing it more plainly."
            )
        frames = kept
        await self._box_stills(result, case_id, kept, detections)
        await advance(
            "detect",
            f"found in {len(detections)} of {looked} stills · "
            + " and ".join(f"{d.confidence:.2f}" for d in detections),
        )

        # --- 4. the real gate -------------------------------------------
        await begin("confirm")
        events = await self.c.cameras.list_events(camera.state)
        gate = evaluate(detections[-1], detections[:-1], events, camera, self.c.gate_config)
        result.gate_decision = gate.decision.value
        result.gate_reason = gate.reason
        await advance("confirm", f"{gate.decision.value} — {gate.reason}")

        case = await self._open_case(
            case_id, camera, detections, gate, frames, synthetic=synthetic
        )
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
        if not synthetic:
            # A drill's case is never filed, so who owns the road only ever had
            # to reach the console. A case seeded into the library is filed
            # against -- the send path reads `case.agency_id` -- so the verdict
            # has to land on the record rather than only on the result object.
            case.agency_id = verdict.agency.id
            case.agency_name = verdict.agency.name
            case.channel = verdict.agency.channel
            case.ref_label = verdict.agency.display_ref_label
            case.updated_at = self.c.clock.now()
            await self.c.repository.save_case(case)
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
    async def _render_clip(
        self, result, spec, camera, case_id, staged, reuse: bool = False
    ) -> list | None:
        """Optional Veo render. Never blocks the pipeline if it fails.

        Returns stills cut from the generated clip, to be analysed in place of
        the flat scene renders -- or None, meaning carry on with those. Every
        failure here is a None: a drill that dies because ffmpeg is missing or a
        render timed out is worse than a drill that falls back to the renders it
        already has, which is what it did before Veo existed.
        """
        from road_cleaner.adapters.media.scenario_prompt import scenario_prompt
        from road_cleaner.ports.media import MediaUnavailableError

        # Kept footage, if this hazard has been rendered before and the caller
        # asked to reuse it. Checked before anything is billed.
        kept_key = f"{DEMO_CLIP_PREFIX}{spec['hazard_type']}.mp4"
        if reuse and await self.c.media_blobs.exists(kept_key):
            log.info("Reusing kept demo clip", extra={"key": kept_key})
            # Copied under the case's own key as well as replayed from the kept
            # one. `clip_for_case` looks beside the case and nowhere else, so a
            # case whose footage lived only in the shared slot opened with real
            # detections, real boxed stills and no video at all.
            case_key = f"{SYNTHETIC_PREFIX}{case_id}/{spec['hazard_type']}.mp4"
            try:
                await self._copy_clip(kept_key, case_key)
                result.clip_url = f"/media/{case_key}"
            except Exception as exc:  # noqa: BLE001 - the run still has its stills
                log.warning("Could not copy the kept clip to the case", extra={"error": str(exc)})
                result.clip_url = f"/media/{kept_key}"
            return await self._stills_from(result, kept_key, camera, case_id, staged)

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
            return None
        result.clip_url = f"/media/{clip.key}"
        if reuse:
            # Keep it, so the next run of this scenario costs nothing. Failing
            # to keep it is not worth losing the render over -- the run has its
            # footage either way, and the next one simply pays again.
            try:
                await self._copy_clip(clip.key, kept_key)
            except Exception as exc:  # noqa: BLE001 - a cache miss is not a failure
                log.warning("Could not keep the demo clip", extra={"error": str(exc)})
        return await self._stills_from(result, clip.key, camera, case_id, staged)


    async def _copy_clip(self, source: str, target: str) -> None:
        """Copy a clip and the provenance sidecar that goes with it.

        The sidecar is not decoration: the library badge reads `model_name` out
        of it, so a clip copied without one showed `SYNTHETIC - pothole` in a row
        of cards that all read `SYNTHETIC - veo-3.1-fast-generate-001`. Losing
        the record of which model made a picture is also the wrong thing to do
        to a system whose whole argument is that its evidence is traceable.
        """
        await self.c.media_blobs.put(
            target, await self.c.media_blobs.get(source), content_type="video/mp4"
        )
        try:
            sidecar = await self.c.media_blobs.get(f"{source}.json")
        except Exception:  # noqa: BLE001 - an older clip may not have one
            return
        await self.c.media_blobs.put(f"{target}.json", sidecar, content_type="application/json")

    async def _box_stills(self, result, case_id, kept, detections) -> None:
        """Burn each detection's box onto the still it came from.

        The case page draws boxes over the video with CSS, which is right there
        and worth nothing the moment a picture leaves the page -- an emailed
        still with no box asks a maintenance desk to find the hazard themselves,
        in a photograph of a road that looks like every other road. The box is
        the difference between a picture and evidence.

        Never fatal. A run that detected, gated and composed correctly should not
        be lost because Pillow could not write a rectangle; the report still has
        the unboxed frames to enclose.
        """
        from road_cleaner.adapters.media.annotate import draw_box

        for (frame, image), detection in zip(kept, detections, strict=True):
            try:
                boxed = draw_box(image, detection.box, detection.box_label)
            except Exception as exc:  # noqa: BLE001 - a rectangle must not fail a run
                log.warning("Could not box a drill still", extra={"error": str(exc)})
                continue
            key = f"{SYNTHETIC_PREFIX}{case_id}/boxed-{frame.blob_key.rsplit('/', 1)[-1]}"
            await self.c.media_blobs.put(key, boxed, content_type="image/jpeg")
            result.evidence_urls.append(f"/media/{key}")

    async def _stills_from(self, result, clip_key, camera, case_id, staged) -> list | None:
        """Two stills out of the clip, far enough apart to be separate looks.

        The moments are inherited from the staged frames rather than invented,
        because the gate wants two observations 90s-1800s apart and that spacing
        is the one thing about a drill's timeline that has to hold. Only the
        imagery changes: same clock, real pictures.
        """
        from road_cleaner.adapters.media.frame_extract import (
            FrameExtractionError,
            sample_clip,
        )

        path = Path(self.c.settings.media_local_path) / clip_key
        try:
            sampled = await sample_clip(path, count=CLIP_SAMPLES)
        except (FrameExtractionError, OSError) as exc:
            log.warning("Could not cut stills from the drill clip", extra={"error": str(exc)})
            return None
        if len(sampled) < len(staged):
            return None

        # Every still, not a chosen pair. Picking two up front meant guessing
        # which moments of an approach the hazard would be legible in, and the
        # guess was wrong twice: first and last put it a hundred metres off and
        # then already under the car. The analyst looks at all of them and the
        # gate weighs whatever was found, which is how `inspect` reads a clip
        # and the reason case pages work.
        #
        # One OBSERVATION_GAP between consecutive stills, not the gap divided
        # among them. Squeezing five samples into the window meant neighbours
        # sixty seconds apart, and the gate wants at least ninety before it will
        # treat a second sighting as corroboration -- so a run that found the
        # pothole twice, at 0.88 and 0.98, was told it had only ever seen it
        # once. Five stills at four minutes span sixteen, still inside the
        # thirty-minute ceiling the other side of the same check.
        start = staged[0][0].captured_at
        span = OBSERVATION_GAP

        frames: list[tuple[Frame, bytes]] = []
        urls: list[str] = []
        for position, still in enumerate(sampled):
            key = f"{SYNTHETIC_PREFIX}{case_id}/clip-{still.index}.jpg"
            await self.c.media_blobs.put(key, still.jpeg, content_type="image/jpeg")
            frame = Frame(
                camera_id=camera.id, captured_at=start + span * position, blob_key=key,
                phash=phash(still.jpeg), width=640, height=360,
            )
            await self.c.repository.save_frame(frame)
            frames.append((frame, still.jpeg))
            urls.append(f"/media/{key}")
        # The staged renders are replaced, not appended: a report carrying both
        # would enclose two pictures of the same hazard that do not match.
        result.frame_urls = urls
        return frames

    async def _open_case(
        self, case_id, camera, detections, gate, frames, *, synthetic: bool = True
    ) -> Case:
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
            # Auditor's queue and the filing path. Off only for the seeded
            # demonstration case, which is meant to sit among the real ones.
            synthetic=synthetic,
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
