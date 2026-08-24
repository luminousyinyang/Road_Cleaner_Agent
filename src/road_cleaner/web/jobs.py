"""Background render jobs for the dashboard.

A Veo render takes about a minute, which is far too long to hold a request open.
So the button starts a job and returns immediately, and the page polls for
progress until a clip appears.

This does relax the rule that generation only ever happens from the CLI, and it
is worth being precise about what is and is not still true:

* Generation still never happens in the polling loop, and never as a side effect
  of loading a page. It takes a deliberate click.
* It still costs money per second of video, so the route refuses unless
  MEDIA_PROVIDER=vertex -- the same explicit opt-in the CLI needs.
* One job per case at a time, so a double-click cannot bill twice.

**On the progress bar:** Veo reports no percentage. The operation is either
running or done. Rather than animate an invented number, jobs expose elapsed
seconds against a typical duration, and the UI labels it an estimate. A progress
bar that claims to know something it does not is worse than one that admits it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from road_cleaner.logging import get_logger
from road_cleaner.ports.media import MediaUnavailableError

log = get_logger(__name__)

# What a render usually takes, measured over the clips generated so far: about
# 65s at 720p, roughly double that at the 1080p default. Used only to scale the
# estimate bar -- nothing depends on it being right, and the bar says "usually".
TYPICAL_SECONDS = 125.0

# Jobs are kept so a page refresh can still see the outcome, then dropped.
_MAX_REMEMBERED = 50


@dataclass
class RenderJob:
    id: str
    case_id: str
    state: str = "running"  # running | done | failed
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    clip_key: str | None = None
    clip_badge: str | None = None
    clip_badge_short: str | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "state": self.state,
            "elapsed": round(self.elapsed, 1),
            # An estimate, and named one. Capped just below full so a bar never
            # sits at 100% while the job is still going.
            "estimated_fraction": (
                1.0
                if self.state != "running"
                else min(0.95, self.elapsed / TYPICAL_SECONDS)
            ),
            "typical_seconds": TYPICAL_SECONDS,
            "clip_key": self.clip_key,
            "clip_url": f"/media/{self.clip_key}" if self.clip_key else None,
            "clip_badge": self.clip_badge,
            "clip_badge_short": self.clip_badge_short,
            "error": self.error,
        }


class RenderJobs:
    """In-process registry of render jobs.

    In-process is the honest scope: this is a single-instance dashboard, and a
    job dies with the worker that started it. Surviving a restart would mean
    persisting operation names and resuming the poll, which is real work for a
    feature whose whole job is to fill a minute of waiting.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, RenderJob] = {}
        self._by_case: dict[str, str] = {}

    def get(self, job_id: str) -> RenderJob | None:
        return self._jobs.get(job_id)

    def active_for(self, case_id: str) -> RenderJob | None:
        job = self._jobs.get(self._by_case.get(case_id, ""))
        return job if job is not None and job.state == "running" else None

    def start(self, container, case_id: str, prompt: str, duration: int) -> RenderJob:
        """Begin a render, or return the one already running for this case."""
        existing = self.active_for(case_id)
        if existing is not None:
            return existing

        job = RenderJob(id=uuid.uuid4().hex[:12], case_id=case_id)
        self._jobs[job.id] = job
        self._by_case[case_id] = job.id
        self._forget_old()
        asyncio.create_task(self._run(container, job, prompt, duration))
        return job

    async def _run(self, container, job: RenderJob, prompt: str, duration: int) -> None:
        try:
            clip = await container.video.render_scenario(
                prompt=prompt, duration_seconds=duration, case_id=job.case_id
            )
        except asyncio.CancelledError:
            # Shutdown mid-render. CancelledError is a BaseException, so it would
            # sail past the handlers below and leave the job stuck reporting
            # "running" -- which the page would poll for forever.
            job.state = "failed"
            job.error = "The server stopped before the render finished."
            job.finished_at = time.monotonic()
            raise
        except MediaUnavailableError as exc:
            job.state, job.error = "failed", str(exc)
            log.warning("Render job failed", extra={"case": job.case_id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a job must never take the app down
            job.state, job.error = "failed", f"Unexpected error: {exc}"
            log.exception("Render job crashed", extra={"case": job.case_id})
        else:
            job.state = "done"
            job.clip_key = clip.key
            job.clip_badge = clip.label
            job.clip_badge_short = clip.short_label
        finally:
            job.finished_at = time.monotonic()

    def _forget_old(self) -> None:
        if len(self._jobs) <= _MAX_REMEMBERED:
            return
        finished = [j for j in self._jobs.values() if j.state != "running"]
        finished.sort(key=lambda j: j.finished_at or 0.0)
        for job in finished[: len(self._jobs) - _MAX_REMEMBERED]:
            self._jobs.pop(job.id, None)
            if self._by_case.get(job.case_id) == job.id:
                self._by_case.pop(job.case_id, None)


# --------------------------------------------------------------- drill jobs


@dataclass
class DrillJob:
    """A drill in flight. Same polling contract as a render job.

    Kept separate from `RenderJob` because the two report different things: a
    render has one opaque wait and no intermediate state, while a drill has six
    stages that each mean something and are the entire point of watching it.
    """

    id: str
    prompt: str
    state: str = "running"  # running | done | failed
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "state": self.state,
            "elapsed": round(self.elapsed, 1),
            "result": self.result,
            "error": self.error,
        }


class DrillJobs:
    """In-process registry of drill runs, mirroring `RenderJobs`."""

    def __init__(self) -> None:
        self._jobs: dict[str, DrillJob] = {}

    def get(self, job_id: str) -> DrillJob | None:
        return self._jobs.get(job_id)

    def start(self, container, prompt: str, *, full: bool = False, pin=None) -> DrillJob:
        job = DrillJob(id=uuid.uuid4().hex[:12], prompt=prompt.strip())
        self._jobs[job.id] = job
        self._forget_old()
        asyncio.create_task(self._run(container, job, full, pin))
        return job

    async def _run(self, container, job: DrillJob, full: bool, pin=None) -> None:
        from road_cleaner.pipeline.drill import Drill, DrillError

        async def on_progress(result) -> None:
            # Publish after every stage so the page can show the pipeline moving
            # rather than a spinner that resolves into a finished answer.
            job.result = result.as_dict()

        try:
            outcome = await Drill(container).run(
                job.prompt, full=full, pin=pin, on_progress=on_progress
            )
        except asyncio.CancelledError:
            job.state = "failed"
            job.error = "The server stopped before the drill finished."
            job.finished_at = time.monotonic()
            raise
        except DrillError as exc:
            job.state, job.error = "failed", str(exc)
            log.warning("Drill failed", extra={"prompt": job.prompt, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a drill must never take the app down
            job.state, job.error = "failed", f"Unexpected error: {exc}"
            log.exception("Drill crashed", extra={"prompt": job.prompt})
        else:
            job.result = outcome.as_dict()
            job.state = "done"
        finally:
            job.finished_at = time.monotonic()

    def _forget_old(self) -> None:
        if len(self._jobs) <= _MAX_REMEMBERED:
            return
        finished = [j for j in self._jobs.values() if j.state != "running"]
        finished.sort(key=lambda j: j.finished_at or 0.0)
        for job in finished[: len(self._jobs) - _MAX_REMEMBERED]:
            self._jobs.pop(job.id, None)


# ------------------------------------------------------------ inspect jobs


@dataclass
class InspectJob:
    """A live analysis of one case's clip.

    Same polling contract as the other two. Keyed by case as well as by id,
    because the expensive part is Vertex calls and two browser tabs pointed at
    the same case must not spend the quota twice.
    """

    id: str
    case_id: str
    state: str = "running"  # running | done | failed
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "state": self.state,
            "elapsed": round(self.elapsed, 1),
            "result": self.result,
            "error": self.error,
        }


class InspectJobs:
    """In-process registry of clip analyses, mirroring `RenderJobs`."""

    def __init__(self) -> None:
        self._jobs: dict[str, InspectJob] = {}
        self._by_case: dict[str, str] = {}

    def get(self, job_id: str) -> InspectJob | None:
        return self._jobs.get(job_id)

    def active_for(self, case_id: str) -> InspectJob | None:
        job = self._jobs.get(self._by_case.get(case_id, ""))
        return job if job is not None and job.state == "running" else None

    def start(self, container, case_id: str) -> InspectJob:
        """Begin an analysis, or hand back the one already running for this case."""
        existing = self.active_for(case_id)
        if existing is not None:
            return existing

        job = InspectJob(id=uuid.uuid4().hex[:12], case_id=case_id)
        self._jobs[job.id] = job
        self._by_case[case_id] = job.id
        self._forget_old()
        asyncio.create_task(self._run(container, job))
        return job

    async def _run(self, container, job: InspectJob) -> None:
        from road_cleaner.pipeline.inspect import InspectError, Inspector

        async def on_progress(result) -> None:
            # Published after every frame, not just every stage: watching the
            # boxes land one at a time is the entire point of the feature.
            job.result = result.as_dict()

        try:
            outcome = await Inspector(container).run(job.case_id, on_progress=on_progress)
        except asyncio.CancelledError:
            job.state = "failed"
            job.error = "The server stopped before the analysis finished."
            job.finished_at = time.monotonic()
            raise
        except InspectError as exc:
            job.state, job.error = "failed", str(exc)
            log.warning("Inspection failed", extra={"case": job.case_id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a job must never take the app down
            job.state, job.error = "failed", f"Unexpected error: {exc}"
            log.exception("Inspection crashed", extra={"case": job.case_id})
        else:
            job.result = outcome.as_dict()
            job.state = "done"
        finally:
            job.finished_at = time.monotonic()

    def _forget_old(self) -> None:
        if len(self._jobs) <= _MAX_REMEMBERED:
            return
        finished = [j for j in self._jobs.values() if j.state != "running"]
        finished.sort(key=lambda j: j.finished_at or 0.0)
        for job in finished[: len(self._jobs) - _MAX_REMEMBERED]:
            self._jobs.pop(job.id, None)
            if self._by_case.get(job.case_id) == job.id:
                self._by_case.pop(job.case_id, None)
