"""Command line for Road Cleaner.

    road-cleaner doctor     what's wired up, and what's missing to go live
    road-cleaner seed       load the camera registry
    road-cleaner demo       run a simulated week and populate the dashboard
    road-cleaner run        run the pipeline against the clock
    road-cleaner serve      start the dashboard
    road-cleaner cases      list cases in the terminal
    road-cleaner outbox     show what would have been sent
    road-cleaner simulate   render synthetic AV footage from a real detection
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
    help="An agent that watches traffic cameras and files the paperwork.",
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


if __name__ == "__main__":
    app()
