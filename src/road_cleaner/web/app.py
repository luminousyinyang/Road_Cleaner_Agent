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

from road_cleaner.agents.auditor import Auditor
from road_cleaner.agents.dispatcher import Dispatcher
from road_cleaner.config import Settings, get_settings
from road_cleaner.container import build_container
from road_cleaner.logging import configure_logging, get_logger
from road_cleaner.ports.blob_store import BlobNotFoundError
from road_cleaner.web import serializers as S

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = build_container(settings, simulated=False)
        await container.startup()
        app.state.container = container
        app.state.auditor = Auditor(container, Dispatcher(container))
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
            {"active": "log", "d": S.case_detail(detail, c.clock.now())},
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
