"""Run the agent over one clip, frame by frame, and stop before sending.

A case page can describe what the system did, or it can do it while you watch.
This is the second thing. Press the button and the clip is decoded into stills,
each still goes to the vision model as a separate call, boxes appear on the
hazard as they come back, the confidence gate rules on what was found, the ADK
reasoner works out whose road it is, and the report composes itself and stops
at a Send button that is deliberately never pressed.

Every stage is the production one. The vision call is `container.vision.analyze`,
the same method the Analyst uses; the gate is `domain.gating.evaluate`; the
jurisdiction lookup is the real resolver with the real ADK fallback; the report
comes out of `narrative` and the agency's own channel. Nothing here is a replay
of a canned answer.

**The one thing that is not the same, stated plainly.** The gate's persistence
rule wants two sightings 90 seconds to 30 minutes apart, because on a fixed
camera that is what separates a hazard from a compression artefact. Five stills
from one eight-second pass cannot satisfy that and it would be a fabrication to
stamp them as though they could -- so this uses `CLIP_GATE`, a `GateConfig`
whose corroboration window is the length of a clip. Every other threshold is
untouched, and the stage says which window it used. See `CLIP_GATE` below.

Nothing here can file. `compose()` is side-effect free by contract and
`transmit()` is never called.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from road_cleaner.adapters.media.frame_extract import (
    FrameExtractionError,
    SampledFrame,
    sample_clip,
)
from road_cleaner.domain import narrative
from road_cleaner.domain.gating import GateConfig, evaluate
from road_cleaner.domain.models import Camera, Case, Detection, Filing, Frame
from road_cleaner.logging import get_logger
from road_cleaner.pipeline.drill import StageReport
from road_cleaner.ports.media import SYNTHETIC_PREFIX

log = get_logger(__name__)

# Five stages, in order. As in `drill.STAGES` there is no sixth: the run ends at
# a composed report on purpose, and a greyed-out "Send" stage would read as
# having run out of time rather than having declined.
STAGES: list[tuple[str, str]] = [
    ("sample", "Sample"),
    ("look", "Look"),
    ("confirm", "Confirm"),
    ("resolve", "Resolve"),
    ("report", "Report"),
]

# How many stills to pull. Five is a compromise with the Vertex quota, which is
# the binding constraint on this whole feature: ~20-30 vision calls before
# RESOURCE_EXHAUSTED. Five frames means a judge can click Run four times.
FRAME_COUNT = 5

# The gate, with its corroboration window narrowed to the length of a clip.
#
# `min_frame_gap_seconds=0` is the honest setting for this input and not a
# loosening of the standard: on a live camera the 90-second floor rules out two
# reads of what is effectively the same instant, whereas five stills spread
# across eight seconds of a moving vehicle are already five genuinely different
# views of the object -- different distance, different angle, different
# lighting. What they cannot tell you is whether the hazard is still there ten
# minutes later, which is the other half of what persistence buys, and the UI
# says so rather than implying this run proves it.
#
# Every other threshold -- the floor, the duplicate radius, the severity bar --
# is left exactly as the live gate has it.
CLIP_GATE = GateConfig(min_frame_gap_seconds=0, max_frame_gap_seconds=60)

# Analyses are cached beside the clip they describe.
CACHE_SUFFIX = ".analysis.json"

# The still saved with a box drawn on it, so the page has real captured evidence
# before anybody presses anything. One per case, overwritten by each run.
EVIDENCE_SUFFIX = ".jpg"


class ComposedPreview(NamedTuple):
    """A finished report and where it would have gone."""

    subject: str
    body: str
    # The real submission target the channel computed: an email address, a
    # maintenance form URL, or an Open311 endpoint. Everything in
    # `seeds/agencies.yaml` is deliberately `example.invalid`, so this opens and
    # cannot arrive -- see the note the page shows beside the Send button.
    destination: str
    channel: str
    # The fields the channel would actually submit: form inputs for a
    # maintenance form, GeoReport attributes for Open311, headers for an email.
    # Shown instead of navigating anywhere -- the filled-in request is the
    # interesting artefact, and a blank third-party form is not.
    payload: dict[str, Any]


class InspectError(RuntimeError):
    """The clip could not be analysed. The message is shown to the user."""


@dataclass
class InspectResult:
    """Everything the case page needs to render one run."""

    stages: list[StageReport]
    case_id: str
    clip_url: str | None = None
    duration_seconds: float = 0.0
    frames: list[dict[str, Any]] = field(default_factory=list)
    evidence_url: str | None = None
    # Where in the clip the saved still came from, so the page can seek to it.
    evidence_at: float | None = None
    gate_decision: str | None = None
    gate_reason: str | None = None
    gate_note: str | None = None
    agency: str | None = None
    agency_rule: str | None = None
    agency_rationale: str | None = None
    report_subject: str | None = None
    report_body: str | None = None
    # Where this would go, and by what route. Surfaced so Send can open the mail
    # client or the agency's intake page rather than explain that it could.
    report_destination: str | None = None
    report_channel: str | None = None
    report_payload: dict[str, Any] = field(default_factory=dict)
    model_name: str | None = None
    # Whether a real vision model looked at these frames, or the local scripted
    # analyzer replayed `seeds/scenarios.json`. Locally `VISION_PROVIDER=auto`
    # resolves to scripted while the deployment sets `gemini`, so the same code
    # produces genuinely different results in the two places and the page must
    # not narrate a replay in the voice of a live call.
    is_scripted: bool = True
    # Always False. Present so the page can assert on it rather than trust a
    # comment, and so the Send button has something to check.
    filed: bool = False
    # True when Vertex could not be reached and a previous run was replayed.
    # Surfaced in the UI -- a cached answer presented as a live one would make
    # this whole feature a mockup, which is the thing it exists to replace.
    from_cache: bool = False
    cache_note: str | None = None
    analyzed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "stages"}
        data["stages"] = [s.as_dict() for s in self.stages]
        return data


def clip_for_case(media_root: Path | str | None, case_id: str) -> Path | None:
    """The newest generated clip for a case, or None if it has none."""
    if media_root is None:
        return None
    folder = Path(media_root) / SYNTHETIC_PREFIX / case_id
    if not folder.is_dir():
        return None
    clips = sorted(
        (p for p in folder.iterdir() if p.suffix.lower() in {".mp4", ".webm"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return clips[0] if clips else None


def cached_analysis(clip: Path) -> dict[str, Any] | None:
    """The last successful run over this clip, if there was one."""
    sidecar = clip.with_name(clip.name + CACHE_SUFFIX)
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text())
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


# Two detections a fraction of a point apart in confidence are, for this
# purpose, equally confident -- so the tie is broken on how big the hazard is in
# frame instead.
CONFIDENCE_TIE = 0.05


def _subject_and_priors(
    found: list[Detection],
) -> tuple[Detection, list[Detection]]:
    """Which observation the gate rules on, and which corroborate it.

    On a live camera the newest look is the one you act on, so `evaluate` takes
    the latest detection and the earlier ones as priors. A clip is not live: it
    is one pass that has already happened, and its last sample is often taken
    after the car has driven past the hazard. Judging the pass by that frame is
    how four frames -- three of them calling a coned-off lane a closure at 0.9 --
    came back as "looked twice and saw different things".

    So the subject is the most confident frame of whatever the pass mostly saw,
    and everything else that agreed with it corroborates. Frames that saw
    something else are neither: they are dropped from the arithmetic, which is
    stricter than counting them, not looser.
    """
    from collections import Counter

    winner = Counter(d.hazard_type for d in found).most_common(1)[0][0]
    agreeing = [d for d in found if d.hazard_type is winner]
    subject = max(agreeing, key=lambda d: d.confidence)
    return subject, [d for d in agreeing if d is not subject]


def _clearest(
    candidates: list[tuple[SampledFrame, Detection]],
) -> tuple[SampledFrame, Detection] | None:
    """The frame to keep as the case's evidence still.

    Not simply the most confident one. A dashcam approaches its hazard, so the
    earliest frame often scores highest on a shape thirty pixels across, while
    the frame two seconds later shows the same object filling a tenth of the
    picture. Someone opening the case page needs to *see* the thing, so among
    frames of comparable confidence the closest look wins.
    """
    if not candidates:
        return None

    def area(detection: Detection) -> float:
        return detection.box.width * detection.box.height if detection.box else 0.0

    ceiling = max(d.confidence for _, d in candidates)
    shortlist = [(s, d) for s, d in candidates if d.confidence >= ceiling - CONFIDENCE_TIE]
    return max(shortlist, key=lambda pair: area(pair[1]))


def destination_for(
    case, agency, settings, *, subject: str = "", body: str = ""
) -> tuple[str, str, dict[str, Any]]:
    """Where a report to this agency would actually go, and by what route.

    Asks the channel rather than reimplementing it: an Open311 destination is the
    endpoint plus `/requests.json`, a maintenance form is the bare endpoint, an
    email is an address. Three rules that already exist in three adapters, and a
    fourth copy here would be the one that drifts.

    Composing is side-effect free by contract -- that is the whole distinction
    between `compose()` and `transmit()`.
    """
    from road_cleaner.container import channel_for_agency

    try:
        channel = channel_for_agency(agency.channel, settings)
        composed = channel.compose(
            # The report has to go in. A maintenance form puts `filing.body` in
            # its `description` field, so composing against an empty Filing
            # produced a form whose only interesting field was blank.
            Filing(
                case_id=case.id,
                agency_id=agency.id,
                channel=agency.channel,
                tier=1,
                subject=subject,
                body=body,
            ),
            case,
            agency,
        )
        return (
            getattr(composed, "destination", "") or "",
            agency.channel.value,
            dict(getattr(composed, "payload", {}) or {}),
        )
    except Exception as exc:  # noqa: BLE001 - a link is not worth an error page
        log.warning("Could not work out a destination", extra={"error": str(exc)})
        return "", agency.channel.value, {}


def _box_dict(detection: Detection) -> dict[str, float] | None:
    if detection.box is None:
        return None
    b = detection.box
    return {"x": b.x, "y": b.y, "width": b.width, "height": b.height}


class Inspector:
    """One analysis run over one case's clip."""

    def __init__(self, container) -> None:
        self.c = container
        self.settings = container.settings

    async def run(self, case_id: str, *, on_progress=None) -> InspectResult:
        """Analyse the case's clip end to end. Never files.

        `on_progress(result)` is called after each stage and after each frame,
        so the page paints results as they land rather than resolving a spinner
        into a finished answer.
        """
        detail = await self.c.repository.get_case_detail(case_id)
        if detail is None:
            raise InspectError(f"No case {case_id}.")
        case, camera = detail.case, detail.camera
        if camera is None:
            raise InspectError(f"{case_id} has no camera on record.")

        clip = clip_for_case(self.settings.media_local_path, case_id)
        if clip is None:
            raise InspectError(
                f"{case_id} has no clip to analyse. Generate one from the library first."
            )

        stages = [StageReport(k, label) for k, label in STAGES]
        result = InspectResult(
            stages=stages,
            case_id=case_id,
            clip_url=f"/media/{SYNTHETIC_PREFIX}{case_id}/{clip.name}",
        )
        by_key = {s.key: s for s in stages}

        async def publish() -> None:
            if on_progress:
                await on_progress(result)

        async def begin(key: str) -> None:
            by_key[key].state = "running"
            await publish()

        async def advance(key: str, detail_text: str = "") -> None:
            by_key[key].state = "done"
            if detail_text:
                by_key[key].detail = detail_text
            await publish()

        try:
            return await self._run_live(
                result, by_key, case, camera, clip, begin, advance, publish
            )
        except InspectError:
            raise
        except Exception as exc:  # noqa: BLE001 - falls back below, or re-raises
            replayed = self._replay(result, by_key, clip, exc)
            if replayed is None:
                log.exception("Inspection failed", extra={"case": case_id})
                raise InspectError(f"Analysis failed: {exc}") from exc
            await publish()
            return replayed

    # ------------------------------------------------------------- the run
    async def _run_live(
        self, result, by_key, case: Case, camera: Camera, clip: Path,
        begin, advance, publish,
    ) -> InspectResult:
        # --- 1. sample ---------------------------------------------------
        await begin("sample")
        try:
            frames = await sample_clip(clip, FRAME_COUNT)
        except FrameExtractionError as exc:
            raise InspectError(str(exc)) from exc
        result.duration_seconds = round(frames[-1].at_seconds, 1)
        await advance(
            "sample",
            f"{len(frames)} stills across {result.duration_seconds:.1f}s of footage",
        )

        # --- 2. look -----------------------------------------------------
        await begin("look")
        detections = await self._look(result, camera, frames, publish)
        found = [d for _, d in detections if d is not None]
        if not found:
            by_key["look"].state = "done"
            by_key["look"].detail = f"Nothing in any of {len(frames)} frames."
            for key in ("confirm", "resolve", "report"):
                by_key[key].state = "blocked"
                by_key[key].detail = "Nothing to report."
            await publish()
            self._cache(clip, result)
            return result

        result.model_name = found[-1].model_name
        result.is_scripted = found[-1].model_name == "scripted"
        await advance(
            "look",
            f"{len(found)} of {len(frames)} frames found something · "
            + " ".join(f"{d.confidence:.2f}" for d in found),
        )

        # --- 3. confirm --------------------------------------------------
        await begin("confirm")
        events = await self.c.cameras.list_events(camera.state)
        subject, priors = _subject_and_priors(found)
        gate = evaluate(subject, priors, events, camera, CLIP_GATE)
        result.gate_decision = gate.decision.value
        result.gate_reason = gate.reason
        result.gate_note = (
            "Agreement here is across one 8-second pass, not two camera polls "
            f"{GateConfig().min_frame_gap_seconds}s apart. On a live camera the "
            "persistence rule would still have to be satisfied separately."
        )
        await advance("confirm", f"{gate.decision.value} — {gate.reason}")

        # --- 4. resolve (ADK) --------------------------------------------
        await begin("resolve")
        verdict = await self.c.jurisdiction.resolve(camera, subject, self.c.reasoner)
        if not verdict.resolved:
            by_key["resolve"].state = "failed"
            by_key["resolve"].detail = verdict.rationale or "No rule matched."
            by_key["report"].state = "blocked"
            by_key["report"].detail = "Held: nobody to send it to."
            await publish()
            self._cache(clip, result)
            return result
        result.agency = verdict.agency.name
        result.agency_rule = verdict.rule_id
        result.agency_rationale = verdict.rationale
        await advance("resolve", f"{verdict.agency.name}  ({verdict.rule_id})")

        # --- 5. compose, and stop ----------------------------------------
        await begin("report")
        preview = self._compose(
            case, camera, found, verdict.agency, self._public(result.evidence_url)
        )
        result.report_subject = preview.subject
        result.report_body = preview.body
        result.report_destination = preview.destination
        result.report_channel = preview.channel
        result.report_payload = preview.payload
        by_key["report"].state = "done"
        by_key["report"].detail = "Draft ready — not sent"
        result.analyzed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        await publish()

        self._cache(clip, result)
        return result

    async def _look(
        self, result, camera: Camera, frames: list[SampledFrame], publish
    ) -> list[tuple[SampledFrame, Detection | None]]:
        """One vision call per still, published as each lands.

        Sequential rather than gathered, unlike frame extraction: the point of
        this stage is to be watched, and five boxes appearing at once is the
        same as not streaming at all. It also keeps the concurrency the
        analyzer's own semaphore expects.
        """
        out: list[tuple[SampledFrame, Detection | None]] = []
        candidates: list[tuple[SampledFrame, Detection]] = []

        for sampled in frames:
            row: dict[str, Any] = {
                "index": sampled.index,
                "at": sampled.at_seconds,
                "stamp": sampled.stamp,
                "state": "looking",
            }
            result.frames.append(row)
            await publish()

            # A Frame row is built because `analyze` takes one, but it is never
            # saved: a still cut out of generated footage is not evidence, and
            # writing it to the frames table would put it one join away from the
            # filing path.
            frame = Frame(
                camera_id=camera.id,
                captured_at=datetime.now().astimezone()
                + timedelta(seconds=sampled.at_seconds),
                blob_key=f"{SYNTHETIC_PREFIX}inspect/{camera.id}/{sampled.index}",
                phash="",
            )
            detection = await self.c.vision.analyze(sampled.jpeg, frame, camera)

            if detection is None:
                row.update({"state": "clear", "found": False, "description": "Nothing here."})
            else:
                row.update(
                    {
                        "state": "found",
                        "found": True,
                        "hazard": detection.hazard_type.value,
                        "hazard_label": detection.hazard_type.value.replace("_", " "),
                        "lane": detection.lane_position,
                        "severity": detection.severity.value,
                        "confidence": round(detection.confidence, 2),
                        "description": detection.description,
                        "box": _box_dict(detection),
                        "box_measured": detection.box_is_measured,
                        "box_label": (
                            f"{detection.hazard_type.value.replace('_', ' ')} · "
                            f"{detection.confidence:.2f}"
                        ),
                        "model": detection.model_name,
                    }
                )
                if detection.box_is_measured:
                    candidates.append((sampled, detection))

            out.append((sampled, detection))
            await publish()

        best = _clearest(candidates)
        if best is not None:
            await self._save_evidence(result, *best)
        return out

    async def _save_evidence(
        self, result, sampled: SampledFrame, detection: Detection
    ) -> None:
        """Write the clearest boxed still, so the page has a picture to show.

        Saved under `SYNTHETIC_PREFIX`, which is what `/media` serves and what
        `is_synthetic_key` recognises -- it is a picture of something a model
        invented, and it is filed where such things go.
        """
        try:
            from road_cleaner.adapters.media.annotate import draw_box

            image = draw_box(sampled.jpeg, detection.box, detection.box_label)
            # One stable key per case rather than one per frame index. Runs
            # pick different frames as the model's confidence shifts, and an
            # indexed name leaves the previous run's still orphaned in the
            # media directory looking exactly as authoritative as the current
            # one.
            key = f"{SYNTHETIC_PREFIX}{result.case_id}/evidence{EVIDENCE_SUFFIX}"
            await self.c.media_blobs.put(key, image, content_type="image/jpeg")
            result.evidence_url = f"/media/{key}"
            result.evidence_at = sampled.at_seconds
        except Exception as exc:  # noqa: BLE001 - a picture must not fail the run
            log.warning("Could not save evidence still", extra={"error": str(exc)})

    # ------------------------------------------------------------- helpers
    def _public(self, path: str | None) -> str | None:
        """A link a road crew could actually open, or nothing.

        `evidence_url` is a site-relative `/media/...` path, which is useless in
        an email. It becomes a link only when `PUBLIC_BASE_URL` says where this
        deployment answers; otherwise the report simply does not mention one,
        because half a URL in a report to an agency is worse than no URL.
        """
        base = (getattr(self.settings, "public_base_url", "") or "").rstrip("/")
        return f"{base}{path}" if base and path else None

    def _compose(self, case, camera, detections, agency, evidence_url=None) -> ComposedPreview:
        """Build the report exactly as the Dispatcher would -- then stop.

        Returns the destination as well as the text. `compose()` works out where
        this would go -- an inbox, a form URL, an Open311 endpoint -- and that
        answer used to be computed and dropped on the next line. It is what lets
        the Send button open the right thing instead of describing it.
        """
        from road_cleaner.container import channel_for_agency

        detection = detections[-1]
        body = narrative.report_body(
            detection,
            case.location,
            narrative.observed_at(detections[0].analyzed_at),
            # Nothing is enclosed with a preview -- the `Filing` below carries no
            # attachments. It used to pass `len(detections)`, so a sweep that
            # found the hazard in three frames told the agency three stills were
            # attached to a message that had none. The marked still is linked
            # instead, which is true and which a DOT can actually open.
            attachment_count=0,
            evidence_url=evidence_url,
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
            return ComposedPreview(subject, body, "", agency.channel.value, {})
        return ComposedPreview(
            getattr(composed, "subject", subject),
            getattr(composed, "body", body),
            getattr(composed, "destination", "") or "",
            agency.channel.value,
            dict(getattr(composed, "payload", {}) or {}),
        )

    def _cache(self, clip: Path, result: InspectResult) -> None:
        """Remember this run so a throttled one later has something to show."""
        try:
            clip.with_name(clip.name + CACHE_SUFFIX).write_text(
                json.dumps(result.as_dict(), indent=2)
            )
        except OSError as exc:  # pragma: no cover - disk trouble, not worth failing
            log.warning("Could not cache analysis", extra={"error": str(exc)})

    def _replay(self, result, by_key, clip: Path, exc: Exception) -> InspectResult | None:
        """Fall back to the last good run, labelled as one.

        Vertex throttles at around twenty to thirty calls, and a judge clicking
        this twice can hit it. The alternative to a labelled replay is an error
        page, which demonstrates nothing. What is not acceptable is replaying
        silently, so `from_cache` is set and the UI shows it.
        """
        cached = cached_analysis(clip)
        if cached is None:
            return None

        replayed = InspectResult(
            stages=[StageReport(**s) for s in cached.get("stages", [])],
            case_id=cached.get("case_id", result.case_id),
        )
        for key, value in cached.items():
            if key != "stages" and hasattr(replayed, key):
                setattr(replayed, key, value)
        replayed.from_cache = True
        replayed.cache_note = (
            f"Vertex was unavailable just now ({type(exc).__name__}), so this is "
            "the last live run over this clip, replayed. Nothing below was "
            "invented for the replay."
        )
        log.warning(
            "Inspection replayed from cache",
            extra={"case": replayed.case_id, "error": str(exc)},
        )
        return replayed
