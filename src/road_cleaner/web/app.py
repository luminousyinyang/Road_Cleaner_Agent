"""The dashboard.

Two public pages -- the road log and how-it-works -- plus a case detail page and
a small JSON API. Server-rendered Jinja rather than a SPA: the mockup is almost
entirely static content, and this keeps the whole product one deployable
artifact with no build step.

The one genuinely interactive thing, "Check now", runs the real Auditor rather
than faking it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from road_cleaner.adapters.media.scenario_prompt import (
    UNSIMULATABLE,
    UnsimulatableHazardError,
    scenario_prompt,
)
from road_cleaner.agents.auditor import Auditor
from road_cleaner.agents.dispatcher import Dispatcher
from road_cleaner.config import MediaProviderKind, Settings, get_settings
from road_cleaner.container import build_container
from road_cleaner.domain.models import Camera, Frame
from road_cleaner.logging import configure_logging, get_logger
from road_cleaner.pipeline.demo_send import SEND_STAGE as DEMO_SEND_STAGE
from road_cleaner.pipeline.drill import STAGES as DRILL_STAGES
from road_cleaner.ports.blob_store import BlobNotFoundError
from road_cleaner.ports.media import is_synthetic_key
from road_cleaner.ports.vision import VisionUnavailableError
from road_cleaner.web import serializers as S
from road_cleaner.web.jobs import DemoSendJobs, DrillJobs, InspectJobs, RenderJobs
from road_cleaner.web.serializers import when

# Offered as one-click starting points. Each is a different hazard class, so a
# judge clicking through sees the gate reach different conclusions rather than
# the same one four times.
DRILL_EXAMPLES = [
    "a mattress in the fast lane on I-85 at rush hour",
    "flooding across both lanes of US-70 after a storm",
    "a deer standing on the shoulder of GA-400 before dawn",
    "a car stopped with hazards on the I-4 shoulder",
]

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class _AssetVersion:
    """Stringifies to a cache-busting token, recomputed each time it renders.

    A plain value captured at import time would go stale the moment a stylesheet
    changed, which is the whole problem this exists to solve.
    """

    def __str__(self) -> str:
        return asset_version()


def asset_version() -> str:
    """A cache-busting token derived from the static files themselves.

    Without this the browser holds on to `app.css` and every style change looks
    like it silently failed -- a page can render with new markup and last week's
    stylesheet, which is worse than either alone because it looks like a bug in
    the markup. Recomputed per request so editing CSS during development shows
    up on reload; the cost is a handful of stat() calls.
    """
    newest = 0.0
    for path in (WEB_DIR / "static").rglob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return str(int(newest))


TEMPLATES.env.globals["v"] = _AssetVersion()

# A phone frame scaled to ~960px and JPEG-encoded lands around 100-200KB. The
# ceiling is generous enough for a full-resolution capture from a careless client
# and small enough that a stuck upload fails fast rather than tying up the worker.
DASHCAM_MAX_BYTES = 2 * 1024 * 1024


def _dashcam_camera() -> Camera:
    """The stand-in for a phone on a windscreen.

    `analyze` needs a Camera because every other caller has a real one, and the
    only fields it reads are the ones that go into the prompt's context line. The
    values here are chosen to make that sentence true rather than plausible: we
    genuinely do not know what road this is or which way it faces, and saying
    "I-285 westbound" to make the prompt read nicely would be inventing evidence.

    It is never stored, so it never collides with a real camera id.
    """
    return Camera(
        id="DASHCAM",
        state="--",
        name="a phone held up to a windscreen",
        road="an unidentified road",
        lat=0.0,
        lng=0.0,
        snapshot_url="dashcam://live",
    )


_MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    # The boxed evidence still an analysis leaves behind. Generated, so it is
    # served from here rather than from `/frames`, which is for what cameras
    # actually saw. Without this it went out as application/octet-stream and
    # rendered only because browsers sniff, which is not something to rely on.
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """Parse a single `bytes=start-end` range. None means "send the whole thing".

    Only the single-range form is handled, which is all a media element sends.
    Anything malformed or unsatisfiable falls back to a 200 with the full body --
    a valid response to any Range request, and better than failing the playback.
    """
    if not header or not header.startswith("bytes=") or total == 0:
        return None
    span = header[len("bytes=") :].split(",")[0].strip()
    start_text, _, end_text = span.partition("-")
    try:
        if not start_text:
            # "bytes=-500" means the *last* 500 bytes.
            length = int(end_text)
            if length <= 0:
                return None
            return max(0, total - length), total - 1
        start = int(start_text)
        end = int(end_text) if end_text else total - 1
    except ValueError:
        return None
    end = min(end, total - 1)
    if start > end or start >= total:
        return None
    return start, end


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = build_container(settings, simulated=False)
        await container.startup()
        app.state.container = container
        app.state.auditor = Auditor(container, Dispatcher(container))
        app.state.renders = RenderJobs()
        app.state.drills = DrillJobs()
        app.state.inspections = InspectJobs()
        app.state.demo_sends = DemoSendJobs()
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(
        title="Road Cleaner",
        description="Autonomous road hazard detection and dispatch.",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.middleware("http")
    async def add_asset_version(request: Request, call_next):
        request.state.asset_version = asset_version()
        return await call_next(request)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:  # noqa: C901 - a flat route table reads better here
    # ------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def scenarios(request: Request, hazard: str = "all"):
        """The scenario library -- the front door.

        Lists *every* confirmed case, not just the ones with a clip. A case with
        no clip yet gets a Generate button; a hazard we refuse to simulate gets a
        card saying so. That is what lets this page replace the old road log
        without losing the cases that never produced footage -- the suppressed,
        the still-watching, and the ones we decline to render.
        """
        c = request.app.state.container
        root = c.settings.media_local_path

        # Hazards we refuse to simulate are left out of the gallery entirely.
        # The case itself stays in the system -- it is a real detection that was
        # reported and cleared, and it still counts in the statistics -- but a
        # library of clips is no place for a card that will never have one.
        cards = []
        for case in await c.repository.list_cases(limit=1000):
            if case.hazard_type.value in UNSIMULATABLE:
                continue
            camera = await c.repository.get_camera(case.camera_id)
            cards.append(S.scenario_card(case, root, camera))

        filters = S.scenario_filters(cards, hazard)
        visible = cards if hazard == "all" else [x for x in cards if x["hazard_key"] == hazard]
        # The pairing at the top needs a case that actually has a clip.
        featured = next((x for x in cards if x["state"] == "clip"), None)

        return TEMPLATES.TemplateResponse(
            request,
            "scenarios.html",
            {
                "active": "scenarios",
                "cards": visible,
                "filters": filters,
                "summary": S.library_summary(cards),
                "featured": featured,
                "stats": S.stat_band(await c.repository.stats(c.clock.now())),
                "veo_model": c.settings.veo_model,
                "gen_enabled": c.settings.media_provider == MediaProviderKind.VERTEX,
                "stages": DRILL_STAGES,
                # The drill's six plus the one it never takes.
                "demo_stages": [*DRILL_STAGES, DEMO_SEND_STAGE],
                "examples": DRILL_EXAMPLES,
            },
        )

    @app.get("/log")
    @app.get("/simulation")
    async def retired_pages():
        """The road log and the old simulation page both folded into `/`.

        Permanent, because these URLs were in the README and the demo script.
        """
        return RedirectResponse("/", status_code=301)

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    async def case_page(request: Request, case_id: str):
        c = request.app.state.container
        detail = await c.repository.get_case_detail(case_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")
        return TEMPLATES.TemplateResponse(
            request,
            "case.html",
            {
                "active": "log",
                "d": S.case_detail(detail, c.clock.now()),
                # Kept out of `case_detail` so the evidence view and the
                # generated view stay separately assembled -- see media_for_case.
                "media": S.media_for_case(c.settings.media_local_path, case_id),
                # The last time the agent was run over this clip, so the page
                # opens on a real result rather than an empty frame.
                "analysis": S.last_analysis(
                    c.settings.media_local_path, case_id, detail, c.settings
                ),
                # A hazard we decline to simulate will never have a clip, so the
                # page says that rather than pointing at a library card that is
                # deliberately not there.
                "unsimulatable": detail.case.hazard_type.value in UNSIMULATABLE,
                "gen_enabled": c.settings.media_provider == MediaProviderKind.VERTEX,
            },
        )

    @app.get("/about", response_class=HTMLResponse)
    async def about(request: Request):
        return TEMPLATES.TemplateResponse(request, "about.html", {"active": "about"})

    # ----------------------------------------------------------- dashcam
    @app.get("/dashcam", response_class=HTMLResponse)
    async def dashcam(request: Request):
        """The same agent, pointed at a real road through a phone."""
        c = request.app.state.container
        return TEMPLATES.TemplateResponse(
            request,
            "dashcam.html",
            {
                "active": "dashcam",
                # The page says which model is about to look, because locally
                # that is the scripted analyzer and it will find nothing.
                "model": getattr(c.vision, "model_name", type(c.vision).__name__),
                "scripted": type(c.vision).__name__ == "ScriptedVisionAnalyzer",
            },
        )

    @app.post("/api/dashcam/look")
    async def dashcam_look(request: Request):
        """One frame from a phone camera, one answer. Nothing is kept.

        Deliberately not a job like the other analysis routes: those exist
        because a Veo render or a five-frame sweep takes a minute, and this is a
        single round trip that a viewfinder is waiting on.

        **Nothing here is written.** No frame, no detection, no case, no filing.
        The `Frame` and `Camera` below are built because `analyze` takes them and
        are then dropped. A phone on a windscreen is not a registered public
        camera, we do not know whose road it is on, and a report backed by a
        picture nobody kept would be unauditable -- so this looks, and stops.
        """
        c = request.app.state.container
        image = await request.body()
        if not image:
            raise HTTPException(status_code=422, detail="No image in the request body.")
        if len(image) > DASHCAM_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"That frame is {len(image) // 1024}KB. Scale it down before "
                    f"sending -- the limit is {DASHCAM_MAX_BYTES // 1024}KB."
                ),
            )
        if not image.startswith(b"\xff\xd8"):
            raise HTTPException(status_code=415, detail="Send a JPEG.")

        camera = _dashcam_camera()
        frame = Frame(camera_id=camera.id, blob_key="", phash="")
        try:
            detection = await c.vision.analyze(image, frame, camera)
        except VisionUnavailableError as exc:
            # Never silently "nothing here" -- a model that could not be reached
            # is not a clear road, and on a viewfinder the difference is the
            # whole point.
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if detection is None:
            return {"found": False, "model": getattr(c.vision, "model_name", "unknown")}
        return {
            "found": True,
            "hazard": detection.hazard_type.value,
            "hazard_label": detection.hazard_type.value.replace("_", " "),
            "severity": detection.severity.value,
            "confidence": round(detection.confidence, 2),
            "description": detection.description,
            "box": (
                {
                    "x": detection.box.x,
                    "y": detection.box.y,
                    "width": detection.box.width,
                    "height": detection.box.height,
                }
                if detection.box
                else None
            ),
            "box_measured": detection.box_is_measured,
            "box_label": (
                f"{detection.hazard_type.value.replace('_', ' ')} · "
                f"{detection.confidence:.2f}"
            ),
            "model": detection.model_name,
        }

    # ------------------------------------------------------------ frames
    @app.get("/frames/{blob_key:path}")
    async def frame(request: Request, blob_key: str):
        """Serve an evidence frame out of the blob store."""
        c = request.app.state.container
        try:
            data = await c.blobs.get(blob_key)
        except (BlobNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="No such frame") from None
        # Frames are immutable once written, so they cache hard.
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # ------------------------------------------------------- simulation
    @app.post("/api/drill")
    async def start_drill(request: Request):
        """Run the whole pipeline against a hazard the user describes.

        Returns a job to poll. The work happens in the background because a
        drill makes several model calls and holding the request open for that
        would be a worse experience than watching the stages arrive.
        """
        body = await request.json()
        prompt = str(body.get("prompt", "")).strip()
        full = bool(body.get("full", False))
        if not prompt:
            raise HTTPException(status_code=422, detail="Describe a hazard first.")
        if len(prompt) > 400:
            raise HTTPException(status_code=422, detail="Keep it under 400 characters.")

        c = request.app.state.container
        if full and c.settings.media_provider != MediaProviderKind.VERTEX:
            raise HTTPException(
                status_code=409,
                detail="Video generation is off. Set MEDIA_PROVIDER=vertex to enable it.",
            )

        # A dropped pin, if there was one. Optional on purpose -- the drill has
        # always been able to invent a location, and the map adds a choice rather
        # than a requirement.
        pin = None
        if body.get("lat") is not None and body.get("lng") is not None:
            from road_cleaner.adapters.geo.places import OutsideCoverageError, locate

            try:
                pin = locate(float(body["lat"]), float(body["lng"]))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=f"Bad pin: {exc}") from exc
            except OutsideCoverageError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        job = request.app.state.drills.start(c, prompt, full=full, pin=pin)
        return JSONResponse(job.as_dict(), status_code=202)

    @app.get("/api/drill/{job_id}")
    async def drill_status(request: Request, job_id: str):
        job = request.app.state.drills.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such drill")
        return job.as_dict()

    # ------------------------------------------------- the demonstration send
    #
    # The only route in this application that causes a message to leave the
    # building. Everything it needs is configuration -- the recipient, that
    # recipient being allowlisted, and SMTP -- and if any of it is missing the
    # route says so plainly rather than starting work it cannot finish.

    def _demo_recipient(c) -> str:
        """The configured recipient, or a 409 explaining exactly what is unset."""
        address = (c.settings.demo_send_to or "").strip()
        if not address:
            raise HTTPException(
                status_code=409,
                detail="DEMO_SEND_TO is not set, so there is no demonstration recipient.",
            )
        if address.lower() not in c.settings.live_filing_allowed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{address} is not in LIVE_FILING_ALLOWLIST. Both are required: "
                    "one names the recipient, the other permits sending to it."
                ),
            )
        if not c.settings.smtp_host:
            raise HTTPException(
                status_code=409, detail="SMTP_HOST is not configured, so nothing can be sent."
            )
        return address

    @app.get("/api/demo/send")
    async def demo_send_ready(request: Request):
        """Whether the live demonstration is configured. Drives the button.

        A GET so the page can ask before showing a control that promises to send
        real mail -- offering it and then failing on a missing setting is worse
        than not offering it.
        """
        c = request.app.state.container
        address = (c.settings.demo_send_to or "").strip()
        ready = bool(
            address
            and address.lower() in c.settings.live_filing_allowed
            and c.settings.smtp_host
        )
        return {
            "ready": ready,
            # Shown on the button so nobody can click it without knowing where
            # it goes. Never the allowlist: that is configuration, not a promise.
            "recipient": address if ready else None,
            # Whether there will be real footage. With Veo off the run falls back
            # to flat scene renders, and the stills a report encloses are
            # coloured rectangles -- worth saying on the page rather than letting
            # somebody discover it in their inbox.
            "veo": c.settings.media_provider == MediaProviderKind.VERTEX,
        }

    @app.post("/api/demo/send")
    async def start_demo_send(request: Request):
        """Run the real pipeline and actually email the report. Returns a job."""
        body = await request.json()
        prompt = str(body.get("prompt", "")).strip()
        full = bool(body.get("full", False))
        if not prompt:
            raise HTTPException(status_code=422, detail="Describe a hazard first.")
        if len(prompt) > 400:
            raise HTTPException(status_code=422, detail="Keep it under 400 characters.")

        c = request.app.state.container
        address = _demo_recipient(c)
        if full and c.settings.media_provider != MediaProviderKind.VERTEX:
            raise HTTPException(
                status_code=409,
                detail="Video generation is off. Set MEDIA_PROVIDER=vertex to enable it.",
            )

        job = request.app.state.demo_sends.start(c, prompt, to=address, full=full)
        return JSONResponse(job.as_dict(), status_code=202)

    @app.get("/api/demo/send/{job_id}")
    async def demo_send_status(request: Request, job_id: str):
        job = request.app.state.demo_sends.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such demonstration run")
        return job.as_dict()

    @app.post("/api/cases/{case_id}/location")
    async def move_case(request: Request, case_id: str):
        """Move a case to a coordinate somebody dropped a pin on.

        A case's location was fixed at the moment it opened, from whatever camera
        saw it. That is right for a camera and wrong for everything else: a clip
        can be re-staged anywhere, and a demo should be able to happen where the
        person watching it lives.

        What moves: the case's location string, its camera's coordinates, and --
        because the road may now belong to somebody else entirely -- the agency.
        What does not move: the trail, the filings, and the detections. Those are
        the record of what happened, and a pin drop is not new evidence about it.
        """
        from road_cleaner.adapters.geo.places import OutsideCoverageError, locate

        c = request.app.state.container
        detail = await c.repository.get_case_detail(case_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="No such case")

        body = await request.json()
        try:
            place = locate(float(body["lat"]), float(body["lng"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"Bad coordinates: {exc}") from exc
        except OutsideCoverageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        case, camera = detail.case, detail.camera
        if camera is not None:
            camera.lat, camera.lng = place.lat, place.lng
            camera.state = place.state
            camera.name = place.short
            # A moved case is no longer at the camera that saw it, so it stops
            # claiming a road name and a district owner it has no basis for.
            camera.road = "an unnamed road"
            camera.direction = None
            camera.county = None
            camera.owner_agency_id = None
            await c.repository.upsert_camera(camera)

        case.location = place.label
        case.state = place.state

        if camera is not None and detail.detections:
            verdict = await c.jurisdiction.resolve(camera, detail.detections[0], c.reasoner)
            if verdict.resolved:
                case.agency_id = verdict.agency.id
                case.agency_name = verdict.agency.name
                case.channel = verdict.agency.channel
                case.ref_label = verdict.agency.display_ref_label
        case.updated_at = c.clock.now()
        await c.repository.save_case(case)

        return {
            "case_id": case.id,
            "location": case.location,
            "state": case.state,
            "agency": case.agency_name,
        }

    @app.post("/api/cases/{case_id}/inspect")
    async def start_inspection(request: Request, case_id: str):
        """Run the agent over this case's clip, frame by frame. Returns a job.

        Costs Vertex quota rather than money, which is why it takes a click and
        why `InspectJobs` collapses concurrent requests for the same case onto
        one run. Unlike the render routes there is no MEDIA_PROVIDER gate: this
        analyses footage that already exists and generates nothing.
        """
        c = request.app.state.container
        if await c.repository.get_case(case_id) is None:
            raise HTTPException(status_code=404, detail="No such case")

        job = request.app.state.inspections.start(c, case_id)
        return JSONResponse(job.as_dict(), status_code=202)

    @app.get("/api/inspect/{job_id}")
    async def inspection_status(request: Request, job_id: str):
        job = request.app.state.inspections.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such analysis")
        return job.as_dict()

    @app.get("/api/where")
    async def where(request: Request, lat: float, lng: float):
        """What is at this coordinate, and who owns the road there.

        The map picker asks this on every pin drop, so it is deliberately small:
        no report is composed and nothing is written. It answers the two
        questions a person dropping a pin actually has -- *where is this* and
        *who would hear about it* -- and refuses, with a reason, when the answer
        is neither.
        """
        from road_cleaner.adapters.geo.places import OutsideCoverageError, locate
        from road_cleaner.domain.enums import HazardType, Severity
        from road_cleaner.domain.models import Detection

        c = request.app.state.container
        try:
            place = locate(lat, lng)
        except OutsideCoverageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        camera = Camera(
            id="PIN",
            state=place.state,
            name=place.short,
            # The sentinel that lets `state-dot-fallback` claim this. A pin has
            # no road name, and pretending otherwise is what the fallback's
            # `road_unknown` matcher exists to prevent.
            road="an unnamed road",
            lat=lat,
            lng=lng,
            snapshot_url="pin://dropped",
            owner_agency_id=None,
        )
        probe = Detection(
            camera_id="PIN", frame_id="", hazard_type=HazardType.DEBRIS,
            lane_position="unknown", severity=Severity.MEDIUM,
            confidence=0.9, description="",
        )
        verdict = await c.jurisdiction.resolve(camera, probe, c.reasoner)

        return {
            "lat": place.lat,
            "lng": place.lng,
            "location": place.label,
            "short": place.short,
            "state": place.state,
            "state_name": place.state_name,
            "nearest": place.nearest,
            "nearest_km": place.nearest_km,
            "agency": verdict.agency.name if verdict.agency else None,
            "agency_id": verdict.agency.id if verdict.agency else None,
            "email": (verdict.agency.email if verdict.agency else None) or None,
            "endpoint": (verdict.agency.endpoint if verdict.agency else None) or None,
            "rule": verdict.rule_id,
        }

    @app.post("/api/dashcam/report")
    async def dashcam_report(request: Request):
        """Turn a dashcam finding into a report addressed to the right agency.

        The phone sends what it saw and where it was; this works out which state
        that coordinate is in, which agency owns roads there, and composes the
        report in the same words the rest of the system uses.

        **It does not send anything, and it stores nothing.** It returns text and
        an address. The last action belongs to whoever is holding the phone --
        their mail app, their thumb.
        """
        from road_cleaner.adapters.geo.places import OutsideCoverageError, locate
        from road_cleaner.domain import narrative
        from road_cleaner.domain.enums import HazardType, Severity
        from road_cleaner.domain.models import Detection

        c = request.app.state.container
        body = await request.json()

        try:
            lat, lng = float(body["lat"]), float(body["lng"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=(
                    "A report needs coordinates. Without them a crew has nowhere "
                    "to go, and there is no way to tell whose road this is."
                ),
            ) from None

        try:
            place = locate(lat, lng)
        except OutsideCoverageError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            hazard = HazardType(body.get("hazard", "debris"))
        except ValueError:
            hazard = HazardType.DEBRIS

        detection = Detection(
            camera_id="DASHCAM",
            frame_id="",
            hazard_type=hazard,
            lane_position="unknown",
            severity=Severity(body.get("severity", "medium")),
            confidence=float(body.get("confidence", 0.0)),
            description=str(body.get("description", "")).strip(),
            model_name=str(body.get("model", "unknown")),
        )

        # A pin has no camera, so it has no owner -- which is exactly what the
        # `state-dot-fallback` rule exists for.
        camera = Camera(
            id="DASHCAM",
            state=place.state,
            name=place.nearest or place.state_name,
            road="an unnamed road",
            lat=lat,
            lng=lng,
            snapshot_url="dashcam://live",
            owner_agency_id=None,
        )
        verdict = await c.jurisdiction.resolve(camera, detection, c.reasoner)
        if not verdict.resolved:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No agency on file for {place.state_name}. Holding rather "
                    "than sending this to the wrong desk."
                ),
            )

        agency = verdict.agency
        report = narrative.report_body(
            detection,
            place.label,
            narrative.observed_at(c.clock.now()),
            attachment_count=1,
            tier=1,
        )
        return {
            "location": place.label,
            "state": place.state,
            "agency": agency.name,
            # Present only when that DOT genuinely publishes an address. Most
            # route through a form instead, and the page says so rather than
            # inventing somewhere for the mail to go.
            "email": agency.email or None,
            "endpoint": agency.endpoint or None,
            "subject": narrative.report_subject(detection, place.short, tier=1),
            "body": report,
        }

    @app.post("/api/simulate/{case_id}")
    async def start_render(request: Request, case_id: str):
        """Kick off a Veo render for one case. Returns a job to poll.

        Refuses unless MEDIA_PROVIDER=vertex. Generation bills per second of
        video, so the dashboard must not be able to spend money that the
        configuration did not explicitly authorise.
        """
        c = request.app.state.container
        if c.settings.media_provider != MediaProviderKind.VERTEX:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Generation is off. Set MEDIA_PROVIDER=vertex to enable it — "
                    "note that it bills per second of video."
                ),
            )

        detail = await c.repository.get_case_detail(case_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")

        first = detail.detections[0] if detail.detections else None
        try:
            prompt = scenario_prompt(
                detail.case,
                detail.camera,
                first.lane_position if first else "",
                first.description if first else "",
            )
        except UnsimulatableHazardError as exc:
            # Not an error the user can fix -- it is a rule. 422 rather than 500.
            raise HTTPException(status_code=422, detail=str(exc)) from None

        job = request.app.state.renders.start(c, case_id, prompt, duration=8)
        return JSONResponse(job.as_dict(), status_code=202)

    @app.get("/api/simulate/jobs/{job_id}")
    async def render_status(request: Request, job_id: str):
        job = request.app.state.renders.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job")
        return job.as_dict()

    @app.get("/media/{blob_key:path}")
    async def media(request: Request, blob_key: str):
        """Serve generated media, with Range support so video can seek.

        Separate from `/frames` on purpose. That route serves camera evidence and
        hardcodes image/jpeg; this one serves things a model made. Keeping them
        apart means a generated clip can never be handed back as an evidence
        frame, and the key prefix is checked rather than assumed.

        Browsers will not scrub a `<video>` without 206 support, and FastAPI's
        plain `Response` does not provide it, hence the manual handling below.
        """
        c = request.app.state.container
        if not is_synthetic_key(blob_key):
            raise HTTPException(status_code=404, detail="Not a generated media key")
        try:
            data = await c.media_blobs.get(blob_key)
        except (BlobNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="No such media") from None

        mime = _MEDIA_MIME.get(Path(blob_key).suffix.lower(), "application/octet-stream")
        total = len(data)
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
            # Generated, and labelled as such even at the transport layer.
            "X-Content-Synthetic": "true",
        }

        span = _parse_range(request.headers.get("range"), total)
        if span is None:
            return Response(content=data, media_type=mime, headers=headers)

        start, end = span
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        return Response(
            content=data[start : end + 1],
            status_code=206,
            media_type=mime,
            headers=headers,
        )

    # --------------------------------------------------------------- api
    # Two paths for one check. Google Front End intercepts `/healthz` on a
    # *.run.app host and answers it with its own 404 before the request reaches
    # the container -- verified against a deployment whose /openapi.json listed
    # /healthz and whose unknown paths correctly returned our own 404 page.
    # `/api/healthz` is the one to use against Cloud Run.
    @app.get("/api/healthz")
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/api/stats")
    async def api_stats(request: Request):
        c = request.app.state.container
        return await c.repository.stats(c.clock.now())

    @app.get("/api/cases")
    async def api_cases(request: Request, kind: str = "all", state: str = "all"):
        c = request.app.state.container
        cases = await c.repository.list_cases(state=state, kind=kind, limit=500)
        return {"cases": [S.case_row(x) for x in cases]}

    @app.get("/api/cases/{case_id}")
    async def api_case(request: Request, case_id: str):
        c = request.app.state.container
        detail = await c.repository.get_case_detail(case_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")
        view = S.case_detail(detail, c.clock.now())
        # The Case model isn't JSON-serialisable wholesale; send what the API needs.
        view["case"] = detail.case.model_dump(mode="json")
        return view

    @app.get("/api/cameras")
    async def api_cameras(request: Request, state: str | None = None):
        c = request.app.state.container
        cameras = await c.repository.list_cameras(state)
        return {"cameras": [x.model_dump(mode="json") for x in cameras]}

    @app.post("/api/cases/{case_id}/recheck")
    async def api_recheck(request: Request, case_id: str):
        """Actually go and look again.

        Pulls a fresh frame, asks whether the hazard from the evidence photo is
        still present, and writes a real trail entry. This is the same code path
        the Auditor runs on its own schedule -- pressing the button just runs it
        now instead of later.
        """
        c = request.app.state.container
        case = await c.repository.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"No case {case_id}")

        # A closed case is not re-opened by looking at it again, so say that
        # rather than running the Auditor and returning a result that changed
        # nothing. Previously this path wrote no trail entry and returned
        # `trail_entry: null`, and the button appeared to do nothing at all.
        if not case.is_open:
            return JSONResponse(
                {
                    "case_id": case_id,
                    "kind": case.kind.value,
                    "still_present": False,
                    "ran": False,
                    "message": (
                        f"This case closed {when(case.closed_at)} and is not re-checked. "
                        "The road was confirmed clear against the original evidence frame."
                    ),
                    "trail_entry": None,
                }
            )

        before = len(await c.repository.get_trail(case_id))
        # Force it to run regardless of the decaying schedule.
        case.next_check_at = None
        updated = await request.app.state.auditor.check_case(case)
        trail = await c.repository.get_trail(case_id)

        entry = None
        if len(trail) > before:
            last = trail[-1]
            entry = {
                "time": last.at.strftime("%a %H:%M:%S"),
                "text": last.text,
                "tone": last.tone.value,
                "stage": last.stage.value.replace("_", " ").title(),
            }

        refreshed = await c.repository.get_case_detail(case_id)
        view = S.case_detail(refreshed, c.clock.now()) if refreshed else {}

        # Always say something true about what just happened. The Auditor can
        # legitimately look and find nothing worth writing down, and "nothing was
        # written to the trail" must not render as "the button is broken".
        still = updated.kind.value != "cleared"
        if entry:
            message = entry["text"]
        elif still:
            message = (
                "Looked again — the hazard is still there, and nothing has changed "
                "since the last check, so there is nothing new to record."
            )
        else:
            message = "Looked again — the road is clear. Closing the case."

        return JSONResponse(
            {
                "case_id": case_id,
                "kind": updated.kind.value,
                "still_present": still,
                "ran": True,
                "message": message,
                "trail_entry": entry,
                "frame_url": view.get("live_frame"),
                "sla": view.get("sla"),
            }
        )

    # ------------------------------------------------------------ errors
    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith(("/api/", "/frames/")):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        return TEMPLATES.TemplateResponse(
            request, "not_found.html", {"active": "log"}, status_code=404
        )


# Uvicorn entry point: `uvicorn road_cleaner.web.app:create_app --factory`
