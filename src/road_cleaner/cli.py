"""Command line for Road Cleaner.

    road-cleaner doctor     what's wired up, and what's missing to go live
    road-cleaner seed       load the camera registry
    road-cleaner demo       run a simulated week and populate the dashboard
    road-cleaner run        run the pipeline against the clock
    road-cleaner serve      start the dashboard
    road-cleaner cases      list cases in the terminal
    road-cleaner outbox     show what would have been sent
    road-cleaner simulate   render synthetic AV footage from a real detection
    road-cleaner reinspect  re-derive cases from their own footage
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from road_cleaner.config import AUTO, Mode, Settings, get_settings
from road_cleaner.container import build_container
from road_cleaner.logging import configure_logging
from road_cleaner.pipeline.runner import PipelineRunner

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="An agent that watches the road and files the paperwork.",
)
console = Console()

KIND_STYLES = {
    "filed": "green",
    "escalated": "red",
    "watching": "yellow",
    "cleared": "dim",
    "suppressed": "dim white",
}


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.mode is Mode.CLOUD)
    return settings


@app.command()
def doctor() -> None:
    """Show which adapter is active for each port, and what live mode still needs."""
    settings = _settings()
    container = build_container(settings)

    table = Table(title="Road Cleaner — wiring", header_style="bold")
    table.add_column("Port")
    table.add_column("Adapter")
    for port, adapter in container.describe().items():
        local = any(
            token in adapter.lower()
            for token in ("local", "memory", "sqlite", "scripted", "fixture", "dry_run")
        )
        table.add_row(port, f"[{'cyan' if local else 'magenta'}]{adapter}[/]")
    console.print(table)

    console.print(
        Panel(
            f"mode = [bold]{settings.mode.value}[/]    "
            f"DRY_RUN = [bold]{'on' if settings.dry_run else 'OFF — reports will be sent'}[/]    "
            f"USE_ADK = [bold]{settings.use_adk}[/]",
            border_style="cyan" if settings.dry_run else "red",
        )
    )

    _print_google_surface(settings, container)

    missing = settings.missing_for_live()
    if missing:
        console.print("\n[yellow]Not yet configured for live operation:[/]")
        for item in missing:
            console.print(f"  · {item}")
        console.print(
            "\n[dim]This is expected for local runs — everything works without them.[/]"
        )
    else:
        console.print("\n[green]All credentials for the selected adapters are present.[/]")


def _print_google_surface(settings: Settings, container) -> None:
    """Which Google models and services this configuration actually uses.

    The wiring table above lists adapter class names, and in a default local run
    every one of them is a local implementation -- which reads as "this project
    uses no Google services" even though the cloud paths are fully built. This
    panel says what is really in play, and names the model ids, so `doctor` is
    usable as evidence rather than as a nine-line denial.
    """
    on = lambda live: "[green]live[/]" if live else "[dim]off[/]"  # noqa: E731

    rows = [
        ("Gemini (Vertex AI)", settings.gemini_model,
         type(container.vision).__name__ == "GeminiVisionAnalyzer"),
        ("Google ADK agents", "jurisdiction + report prose", settings.use_adk),
        ("Veo", settings.veo_model, str(settings.media_provider) == "vertex"),
        ("Chirp 3 HD", settings.tts_voice, str(settings.media_provider) == "vertex"),
        ("Lyria", settings.lyria_model, str(settings.media_provider) == "vertex"),
        ("Firestore", settings.firestore_database, str(settings.repository) == "firestore"),
        ("Cloud Storage", settings.gcs_bucket or "—", str(settings.blob_store) == "gcs"),
        ("Pub/Sub", "frame.captured / hazard.confirmed", str(settings.event_bus) == "pubsub"),
    ]

    table = Table(title="Google models and services", header_style="bold")
    table.add_column("Service")
    table.add_column("Model / target")
    table.add_column("Status")
    for name, target, live in rows:
        table.add_row(name, f"[dim]{target}[/]", on(live))
    console.print()
    console.print(table)

    project = settings.google_cloud_project or "[red]unset[/]"
    # Two lines rather than one: the single-line form wrapped mid-value in an
    # 80-column terminal, which is exactly where this will be read from.
    console.print(f"[dim]project[/]  {project}")
    console.print(
        f"[dim]regions[/]  gemini {settings.google_cloud_location}"
        f"  ·  media {settings.vertex_media_location}"
    )


@app.command()
def seed() -> None:
    """Load the camera registry into the store."""
    asyncio.run(_seed())


async def _seed() -> None:
    container = build_container(_settings())
    await container.startup()
    try:
        runner = PipelineRunner(container)
        count = await runner.seed()
        console.print(f"[green]Seeded {count} cameras.[/]")
    finally:
        await container.shutdown()


@app.command()
def demo(
    days: Annotated[float, typer.Option(help="Simulated days of road time to run.")] = 7.0,
    step: Annotated[int, typer.Option(help="Simulated seconds per tick.")] = 300,
    reset: Annotated[bool, typer.Option(help="Wipe existing data first.")] = True,
) -> None:
    """Run a simulated week and populate the dashboard.

    Uses a frozen clock, so a week of road time passes in seconds and the
    twenty-six-hour escalation path actually executes.
    """
    asyncio.run(_demo(days, step, reset))


def _reset_state(settings: Settings) -> None:
    """Wipe everything a previous run produced. All of it is regenerable.

    Tolerant of failure on purpose. This is cleanup of disposable data, and a
    directory that will not delete (a stale handle, an editor watching the tree,
    a filesystem race on a nested tree) must not abort the run and leave the
    caller with no database at all.
    """
    import shutil
    from pathlib import Path

    db = settings.sqlite_path
    if db:
        # The -wal and -shm siblings matter: leaving a WAL behind next to a
        # freshly created database is how you get an unreadable store.
        for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            if path.exists():
                path.unlink(missing_ok=True)

    for directory in (settings.blob_local_path, settings.filing_outbox):
        if directory and Path(directory).exists():
            shutil.rmtree(directory, ignore_errors=True)

    settings.ensure_directories()


async def _demo(days: float, step: int, reset: bool) -> None:
    settings = _settings()

    if reset:
        # Everything the previous run wrote, not just the database. Leaving the
        # outbox behind makes it look like reports were filed twice, and leaving
        # frames behind fills the disk with evidence for cases that no longer
        # exist.
        _reset_state(settings)

    container = build_container(settings, simulated=True, run_days=days)
    await container.startup()
    try:
        runner = PipelineRunner(container)
        cameras = await runner.seed()
        console.print(f"[dim]Seeded {cameras} cameras. Running {days:g} simulated days…[/]")

        minutes = int(days * 24 * 60)
        with console.status("[cyan]Watching…") as status:
            def progress(done: int, total: int) -> None:
                status.update(f"[cyan]Watching… tick {done}/{total}")

            stats = await runner.run_simulated(
                minutes=minutes, step_seconds=step, progress=progress
            )

        _print_stats(stats)
        console.print(
            "\nRun [bold]road-cleaner serve[/] to see the road log, "
            "or [bold]road-cleaner outbox[/] to read what would have been sent."
        )
    finally:
        await container.shutdown()


def _print_stats(stats) -> None:
    table = Table(title="What happened", header_style="bold")
    table.add_column("")
    table.add_column("", justify="right")

    table.add_row("Camera polls", f"{stats.polls:,}")
    table.add_row("  unchanged, skipped", f"{stats.frames_skipped:,} ({stats.skip_rate:.0%})")
    table.add_row("  cameras offline", f"{stats.camera_failures:,}")
    table.add_row("Frames published", f"{stats.frames_published:,}")
    table.add_row(
        "  killed by prefilter", f"{stats.prefilter_kills:,} ({stats.prefilter_rate:.0%})"
    )
    table.add_row("  sent to vision", f"{stats.frames_analyzed:,}")
    table.add_row("Detections", f"{stats.detections:,}")
    table.add_row("Reports filed", f"{stats.cases_filed:,}")
    table.add_row("Escalated", f"{stats.escalated:,}")
    table.add_row("Flagged for a human", f"{stats.flagged_for_human:,}")
    table.add_row("Cleared", f"{stats.cleared:,}")
    if stats.unresolved:
        table.add_row("Held (no agency found)", f"{stats.unresolved:,}")
    console.print(table)

    if stats.by_kind:
        line = "  ".join(
            f"[{KIND_STYLES.get(k, 'white')}]{v} {k}[/]" for k, v in sorted(stats.by_kind.items())
        )
        console.print(Panel(line, title="Cases", border_style="dim"))


@app.command()
def run(
    minutes: Annotated[int, typer.Option(help="Simulated minutes to run. 0 = forever.")] = 0,
    step: Annotated[int, typer.Option(help="Seconds per tick.")] = 60,
    once: Annotated[bool, typer.Option(help="One polling pass, then exit.")] = False,
) -> None:
    """Run the pipeline.

    `--once` is what Cloud Run Jobs use: a single pass, then a clean exit. A job
    that never returns is a job that runs until its timeout kills it.
    """
    asyncio.run(_run(minutes, step, once))


async def _run(minutes: int, step: int, once: bool) -> None:
    settings = _settings()
    container = build_container(settings, simulated=minutes > 0)
    await container.startup()
    try:
        runner = PipelineRunner(container)
        await runner.seed()

        if once:
            await runner.wire()
            published = await runner.watcher.tick()
            await container.bus.drain()
            console.print(f"[green]Polled the fleet. {published} frames published.[/]")
        elif minutes > 0:
            stats = await runner.run_simulated(minutes=minutes, step_seconds=step)
            _print_stats(stats)
        else:
            console.print("[cyan]Running. Ctrl-C to stop.[/]")
            await runner.run_forever(step_seconds=step)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")
    finally:
        await container.shutdown()


@app.command()
def audit() -> None:
    """Re-check every open case once, then exit.

    The Auditor's scheduled entry point. Closes what has cleared, escalates what
    is overdue, and hands anything past two deadlines to a human.
    """
    asyncio.run(_audit())


async def _audit() -> None:
    from road_cleaner.agents.auditor import Auditor
    from road_cleaner.agents.dispatcher import Dispatcher

    container = build_container(_settings(), simulated=False)
    await container.startup()
    try:
        auditor = Auditor(container, Dispatcher(container))
        checked = await auditor.tick()
        await container.bus.drain()
        console.print(
            f"[green]Re-checked {checked} case(s).[/] "
            f"cleared={auditor.cleared} escalated={auditor.escalated} "
            f"flagged={auditor.flagged_for_human}"
        )
    finally:
        await container.shutdown()


@app.command()
def cases(
    state: Annotated[str, typer.Option(help="GA, FL, NC or all.")] = "all",
    kind: Annotated[str, typer.Option(help="filed, watching, escalated, cleared, all.")] = "all",
) -> None:
    """List cases."""
    asyncio.run(_cases(state, kind))


async def _cases(state: str, kind: str) -> None:
    container = build_container(_settings())
    await container.startup()
    try:
        rows = await container.repository.list_cases(state=state, kind=kind, limit=200)
        if not rows:
            console.print("[dim]No cases. Run `road-cleaner demo` first.[/]")
            return
        table = Table(header_style="bold")
        table.add_column("Case")
        table.add_column("Status")
        table.add_column("Hazard")
        table.add_column("Where")
        table.add_column("Reference")
        for case in rows:
            style = KIND_STYLES.get(case.kind.value, "white")
            table.add_row(
                case.id,
                f"[{style}]{case.kind.value}[/]",
                case.hazard_title,
                case.location,
                case.reference or "—",
            )
        console.print(table)
    finally:
        await container.shutdown()


@app.command()
def outbox(
    show: Annotated[int, typer.Option(help="How many reports to print in full.")] = 1,
) -> None:
    """Show the reports that would have been sent."""
    settings = _settings()
    files = sorted(settings.filing_outbox.glob("*.txt"))
    if not files:
        console.print("[dim]Outbox is empty. Run `road-cleaner demo` first.[/]")
        return
    console.print(f"[bold]{len(files)}[/] composed reports in {settings.filing_outbox}\n")
    for path in files[-show:]:
        console.print(Panel(path.read_text(), title=path.name, border_style="dim"))
    if len(files) > show:
        console.print(f"[dim]…and {len(files) - show} more. Use --show N to see them.[/]")


@app.command()
def simulate(
    case: Annotated[str, typer.Option(help="Case id to build a scenario from.")] = "",
    limit: Annotated[
        int, typer.Option(help="How many cases to render, when --case is omitted.")
    ] = 1,
    duration: Annotated[int, typer.Option(help="Clip length in seconds.")] = 8,
    provider: Annotated[
        str, typer.Option(help="scripted (replay cached) or vertex (really call Veo).")
    ] = "",
    seed_frame: Annotated[
        bool, typer.Option(help="Seed image-to-video from the stored evidence frame.")
    ] = False,
    video: Annotated[
        bool,
        typer.Option(help="Render the clip. --no-video re-does audio without paying for Veo."),
    ] = True,
    narrate: Annotated[
        bool, typer.Option(help="Also render the spoken briefing with Chirp.")
    ] = False,
    score: Annotated[bool, typer.Option(help="Also render a Lyria bed for the reel.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Print the prompts without generating.")] = False,
) -> None:
    """Render synthetic AV training footage from real detections.

    Takes a hazard the fleet actually found, and asks Veo to show the same
    scenario from a dashcam instead of a fixed pole-mounted camera -- the
    perspective a perception stack trains on, of an edge case that genuinely
    occurred.

    This is the only place generation happens. It is never on a request path and
    never in the polling loop, because Veo bills per second of video and takes
    tens of seconds per clip.

    Everything it writes is labelled synthetic and stored apart from the evidence
    frames. None of it can reach a filed report.
    """
    if provider:
        # Settings are cached and read from the environment, so an override has
        # to go through the environment too.
        import os

        from road_cleaner.config import reset_settings_cache

        os.environ["MEDIA_PROVIDER"] = provider
        reset_settings_cache()
    asyncio.run(_simulate(case, limit, duration, seed_frame, video, narrate, score, dry_run))


async def _simulate(
    case_id: str,
    limit: int,
    duration: int,
    seed_frame: bool,
    want_video: bool,
    narrate: bool,
    score: bool,
    dry_run: bool,
) -> None:
    from road_cleaner.adapters.media.scenario_prompt import (
        UnsimulatableHazardError,
        scenario_prompt,
    )
    from road_cleaner.ports.blob_store import BlobNotFoundError
    from road_cleaner.ports.media import MediaUnavailableError

    settings = _settings()
    container = build_container(settings)
    await container.startup()
    try:
        if case_id:
            detail = await container.repository.get_case_detail(case_id)
            if detail is None:
                console.print(f"[red]No such case:[/] {case_id}")
                raise typer.Exit(1)
            details = [detail]
        else:
            rows = await container.repository.list_cases(state="all", kind="all", limit=50)
            if not rows:
                console.print("[dim]No cases. Run `road-cleaner demo` first.[/]")
                raise typer.Exit(1)
            details = []
            for row in rows:
                if len(details) >= limit:
                    break
                got = await container.repository.get_case_detail(row.id)
                if got is not None:
                    details.append(got)

        console.print(
            f"[bold]{type(container.video).__name__}[/] · "
            f"writing to [dim]{settings.media_local_path}[/]\n"
        )

        for detail in details:
            current = detail.case
            first = detail.detections[0] if detail.detections else None
            lane = first.lane_position if first else ""
            try:
                prompt = scenario_prompt(
                    current, detail.camera, lane, first.description if first else ""
                )
            except UnsimulatableHazardError as exc:
                console.print(f"[yellow]{current.id} skipped:[/] {exc}")
                continue

            console.print(Panel(prompt, title=f"{current.id} · prompt", border_style="dim"))
            if dry_run:
                continue

            # Seeding image-to-video anchors the clip to the road that was
            # actually photographed -- but only when the frame is a photograph.
            # Off by default because in fixture mode it is not: the frames are
            # flat-shaded synthetic renders with the camera id and timestamp
            # burned into them, and Veo faithfully reproduces both, yielding a
            # cartoon with doubled overlay text. Worth turning on once the
            # camera source is a real 511 feed; useless before then.
            seed = None
            mark = next((f for f in current.frame_refs if f.mark and f.blob_key), None)
            if seed_frame and mark is not None:
                try:
                    seed = await container.blobs.get(mark.blob_key)
                except BlobNotFoundError:
                    console.print(f"[yellow]Evidence frame missing for {current.id}[/]")

            if want_video:
                try:
                    clip = await container.video.render_scenario(
                        prompt=prompt,
                        seed_image=seed,
                        duration_seconds=duration,
                        frame_id=mark.blob_key if mark else None,
                        case_id=current.id,
                    )
                except MediaUnavailableError as exc:
                    console.print(f"[red]{current.id} failed:[/] {exc}")
                    continue
                console.print(
                    f"  [green]clip[/] {clip.key}  "
                    f"[dim]{clip.size_bytes / 1_000_000:.1f} MB · {clip.label}[/]"
                )

            if narrate:
                text = (current.explain or current.sentence or "").strip()
                try:
                    voice = await container.speech.narrate(text, case_id=current.id)
                except MediaUnavailableError as exc:
                    console.print(f"  [red]narration failed:[/] {exc}")
                else:
                    console.print(f"  [green]voice[/] {voice.key}")

        if score and not dry_run:
            try:
                bed = await container.music.score(
                    "Restrained ambient instrumental underscore for a documentary "
                    "about road maintenance. Sparse, unhurried, no drums.",
                )
            except MediaUnavailableError as exc:
                console.print(f"[red]score failed:[/] {exc}")
            else:
                console.print(f"[green]score[/] {bed.key}")
    finally:
        await container.shutdown()


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "",
    port: Annotated[int, typer.Option()] = 0,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes.")] = False,
) -> None:
    """Start the dashboard."""
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "road_cleaner.web.app:create_app",
        factory=True,
        host=host or settings.web_host,
        port=port or settings.web_port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command()
def config() -> None:
    """Print the resolved configuration, with secrets masked."""
    settings = _settings()
    table = Table(header_style="bold")
    table.add_column("Setting")
    table.add_column("Value")
    for name, value in sorted(settings.model_dump().items()):
        if value is None or value == "" or value == AUTO:
            rendered = "[dim]—[/]"
        elif any(s in name for s in ("key", "password", "secret", "token")):
            rendered = "[green]set[/]"
        else:
            rendered = str(value)
        table.add_row(name, rendered)
    console.print(table)


REINSPECT_HELP = """Re-derive each case from the clip attached to it.

Runs the real analysis over every case that has footage and caches the result
beside the clip, so each case page opens on a genuine Gemini run with a real
box rather than on a button.

Where the clip disagrees with the case -- the record says `animal`, the model
sees `debris` -- the disagreement is printed. `--adopt` then rewrites the case
to match its own footage, which is what makes the page true by construction.
Without it nothing in the database is touched.

Vertex throttles at roughly twenty to thirty vision calls, and this makes five
per case, so it paces itself and skips work already cached. Re-run it until it
reports nothing left to do."""


@app.command(help=REINSPECT_HELP)
def reinspect(
    case_id: Annotated[
        str, typer.Option("--case", help="Just this one case.")
    ] = "",
    adopt: Annotated[
        bool,
        typer.Option("--adopt", help="Rewrite each case to match what its clip shows."),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-run cases that are already cached.")
    ] = False,
    pause: Annotated[
        float, typer.Option("--pause", help="Seconds to wait between cases.")
    ] = 8.0,
) -> None:
    asyncio.run(_reinspect(case_id, adopt, force, pause))


async def _reinspect(case_id: str, adopt: bool, force: bool, pause: float) -> None:
    from road_cleaner.domain import narrative
    from road_cleaner.pipeline.inspect import (
        InspectError,
        Inspector,
        cached_analysis,
        clip_for_case,
    )

    settings = _settings()
    container = build_container(settings)
    await container.startup()

    if adopt:
        _warn_before_rewriting(settings)

    try:
        cases = await container.repository.list_cases(limit=500)
        if case_id:
            cases = [c for c in cases if c.id == case_id]
            if not cases:
                console.print(f"[red]No case {case_id}.[/]")
                raise typer.Exit(1)

        table = Table(header_style="bold")
        for column in ("Case", "On record", "In the clip", "Verdict"):
            table.add_column(column)

        skipped = done = 0
        for case in cases:
            clip = clip_for_case(settings.media_local_path, case.id)
            if clip is None:
                table.add_row(case.id, case.hazard_type.value, "[dim]no clip[/]", "[dim]skipped[/]")
                continue
            # A cached run is a real run -- it just already happened. Adopting
            # from it costs no quota, and skipping the whole case because it was
            # cached meant `--adopt` quietly did nothing on exactly the cases
            # that had already been analysed, which is most of them.
            cached = None if force else cached_analysis(clip)
            if cached is not None:
                result = _Cached(cached)
                skipped += 1
            else:
                try:
                    result = await Inspector(container).run(case.id)
                except InspectError as exc:
                    table.add_row(
                        case.id, case.hazard_type.value, f"[red]{exc}[/]", "[red]failed[/]"
                    )
                    continue

            found = [f for f in result.frames if f.get("found")]
            if not found:
                table.add_row(
                    case.id, case.hazard_type.value, "[dim]nothing[/]", "[yellow]no hazard[/]"
                )
                continue

            # What the clip mostly shows, rather than what the last frame
            # happened to say -- a single outlying frame should not redefine a
            # case when the other four agree.
            seen = _most_common(f["hazard"] for f in found)
            # Captured before adopting, which mutates the case in place --
            # otherwise the row reads "debris -> debris, adopted" and hides the
            # one thing the table exists to show.
            was = case.hazard_type.value
            agrees = seen == was
            verdict = "[green]agrees[/]" if agrees else "[yellow]differs[/]"

            # A case can agree about the hazard and still be titled wrongly: the
            # title is written once and persisted, so a case adopted while
            # `hazard_title` still appended lane numbers is stored as "Debris in
            # lane 2" and nothing recomputes it. Adopt on a stale title too.
            stale_title = case.hazard_title != narrative.HAZARD_TITLES.get(
                case.hazard_type, case.hazard_title
            )
            if adopt and (not agrees or stale_title):
                await _adopt(container, case, result, found, seen, narrative)
                verdict = "[cyan]adopted[/]" if not agrees else "[cyan]retitled[/]"

            table.add_row(case.id, was, seen, verdict)
            if cached is None:
                done += 1
                # Only pace after real calls. A cached case made none.
                if pause and case is not cases[-1]:
                    await asyncio.sleep(pause)

        console.print(table)
        console.print(
            f"[green]{done} analysed[/]"
            + (f", [dim]{skipped} read from cache (use --force to re-run)[/]" if skipped else "")
        )
        if not adopt:
            console.print("[dim]Nothing in the database was changed. Use --adopt to rewrite.[/]")
    finally:
        await container.shutdown()


class _Cached:
    """A cached analysis, wearing the shape `_adopt` expects.

    Only the three fields it reads. A dict would do, but then every access
    would be a string key and the difference between a fresh run and a replayed
    one would be invisible at the call site.
    """

    def __init__(self, data: dict) -> None:
        self.frames = data.get("frames", [])
        self.clip_url = data.get("clip_url")
        self.model_name = data.get("model_name")


def _warn_before_rewriting(settings: Settings) -> None:
    """Say what is about to be overwritten, and where the copy is.

    This command rewrites case records in place. The database holds a demo week
    that cannot be regenerated identically, so a copy is taken first rather than
    trusting that the run goes well.
    """
    import shutil
    from datetime import datetime
    from pathlib import Path

    source = Path(settings.sqlite_path)
    if not source.is_file():
        return
    backup = source.with_name(f"{source.stem}.before-reinspect-"
                              f"{datetime.now():%Y%m%dT%H%M%S}{source.suffix}")
    shutil.copy2(source, backup)
    console.print(f"[dim]Copied the database to {backup.name} first.[/]")


def _most_common(values) -> str:
    from collections import Counter

    return Counter(values).most_common(1)[0][0]


async def _adopt(container, case, result, found, seen, narrative) -> None:
    """Rewrite one case to match what its own footage shows.

    Only the fields the clip is evidence about: what the hazard is, how sure we
    are, and where it sits in frame. The trail, the filings and the agency are
    the record of what the system actually did at the time, and rewriting those
    to match a later analysis would be falsifying history rather than correcting
    a description.
    """
    import json

    from road_cleaner.domain.enums import HazardType, Severity
    from road_cleaner.domain.models import BoundingBox, Detection

    best = max(
        (f for f in found if f["hazard"] == seen),
        key=lambda f: f.get("confidence", 0.0),
    )
    hazard = HazardType(seen)

    # Rebuilt so the headline comes out of `narrative.hazard_title` -- the same
    # function that titled the case in the first place. Phrasing a title by hand
    # here is how two places start disagreeing about what to call the same thing.
    observed = Detection(
        camera_id=case.camera_id,
        frame_id="",
        hazard_type=hazard,
        lane_position=best.get("lane", "unknown"),
        severity=Severity(best.get("severity", case.severity.value)),
        confidence=float(best.get("confidence", case.confidence)),
        description=best.get("description", ""),
        box=BoundingBox(**best["box"]) if best.get("box") else case.box,
        box_is_measured=bool(best.get("box_measured")),
        model_name=result.model_name or "unknown",
    )

    case.hazard_type = observed.hazard_type
    case.confidence = observed.confidence
    case.severity = observed.severity
    case.hazard_title = narrative.hazard_title(observed)
    case.box = observed.box
    case.box_label = observed.box_label
    case.raw_model_json = json.dumps(
        {
            "source": "reinspect",
            "clip": result.clip_url,
            "model": result.model_name,
            "frames": result.frames,
        },
        indent=2,
    )
    case.updated_at = container.clock.now()
    await container.repository.save_case(case)


if __name__ == "__main__":
    app()
