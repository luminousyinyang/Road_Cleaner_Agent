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
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from road_cleaner.adapters.media.scenario_prompt import (
    UnsimulatableHazardError,
    scenario_prompt,
)
from road_cleaner.agents.auditor import Auditor
from road_cleaner.agents.dispatcher import Dispatcher
from road_cleaner.config import MediaProviderKind, Settings, get_settings
from road_cleaner.container import build_container
from road_cleaner.logging import configure_logging, get_logger
from road_cleaner.ports.blob_store import BlobNotFoundError
from road_cleaner.ports.media import is_synthetic_key
from road_cleaner.web import serializers as S
from road_cleaner.web.jobs import RenderJobs

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_MEDIA_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
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
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:  # noqa: C901 - a flat route table reads better here
    # ------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def road_log(request: Request, kind: str = "all", state: str = "all"):
        c = request.app.state.container
        now = c.clock.now()

        every = await c.repository.list_cases(limit=1000)
        counts_by_kind: dict[str, int] = {}
        counts_by_state: dict[str, int] = {}
        for case in every:
            counts_by_kind[case.kind.value] = counts_by_kind.get(case.kind.value, 0) + 1
            counts_by_state[case.state] = counts_by_state.get(case.state, 0) + 1

        visible = [
            case
            for case in every
            if (kind == "all" or case.kind.value == kind)
            and (state == "all" or case.state == state)
        ]

        def href(new_kind: str, new_state: str) -> str:
            return f"/?kind={new_kind}&state={new_state}#log"

        filters = [
            {
                **f,
                "count": len(every) if f["key"] == "all" else counts_by_kind.get(f["key"], 0),
                "href": href(f["key"], state),
            }
            for f in S.FILTERS
        ]
        states = [
            {
                **s,
                "count": len(every) if s["key"] == "all" else counts_by_state.get(s["key"], 0),
                "href": href(kind, s["key"]),
            }
            for s in S.STATES
        ]

        return TEMPLATES.TemplateResponse(
            request,
            "log.html",
            {
                "active": "log",
                "cases": [S.case_row(x) for x in visible],
                "filters": filters,
                "states": states,
                "active_filter": kind,
                "active_state": state,
                "summary": S.summary_line(counts_by_kind),
                "stats": S.stat_band(await c.repository.stats(now)),
            },
        )

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
                "gen_enabled": c.settings.media_provider == MediaProviderKind.VERTEX,
            },
        )

    @app.get("/about", response_class=HTMLResponse)
    async def about(request: Request):
        return TEMPLATES.TemplateResponse(request, "about.html", {"active": "about"})

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
    @app.get("/simulation", response_class=HTMLResponse)
    async def simulation(request: Request):
        """Real detections beside the synthetic footage generated from them."""
        c = request.app.state.container
        pairs = []
        for case in await c.repository.list_cases(limit=1000):
            media = S.media_for_case(c.settings.media_local_path, case.id)
            if media:
                pairs.append({"case": S.case_row(case), "media": media})
        return TEMPLATES.TemplateResponse(
            request,
            "simulation.html",
            {
                "active": "simulation",
                "pairs": pairs,
                "score": S.media_for_case(c.settings.media_local_path, "score"),
                "veo_model": c.settings.veo_model,
                "tts_voice": c.settings.tts_voice,
                "lyria_model": c.settings.lyria_model,
                "gen_enabled": c.settings.media_provider == MediaProviderKind.VERTEX,
            },
        )

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

        return JSONResponse(
            {
                "case_id": case_id,
                "kind": updated.kind.value,
                "still_present": updated.kind.value != "cleared",
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
