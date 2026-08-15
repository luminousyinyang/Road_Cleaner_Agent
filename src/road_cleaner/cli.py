"""Command line for Road Cleaner.

    road-cleaner doctor     what's wired up, and what's missing to go live
    road-cleaner seed       load the camera registry
    road-cleaner demo       run a simulated week and populate the dashboard
    road-cleaner run        run the pipeline against the clock
    road-cleaner serve      start the dashboard
    road-cleaner cases      list cases in the terminal
    road-cleaner outbox     show what would have been sent
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
    """Wipe everything a previous run produced. All of it is regenerable."""
    import shutil
    from pathlib import Path

    db = settings.sqlite_path
    if db:
        for path in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
            if path.exists():
                path.unlink()

    for directory in (settings.blob_local_path, settings.filing_outbox):
        if directory and Path(directory).exists():
            shutil.rmtree(directory)
    settings.ensure_directories()


async def _demo(days: float, step: int, reset: bool) -> None:
    settings = _settings()

    if reset:
        # Everything the previous run wrote, not just the database. Leaving the
        # outbox behind makes it look like reports were filed twice, and leaving
        # frames behind fills the disk with evidence for cases that no longer
        # exist.
        _reset_state(settings)

    container = build_container(settings, simulated=True)
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
) -> None:
    """Run the pipeline. With --minutes it runs on simulated time and stops."""
    asyncio.run(_run(minutes, step))


async def _run(minutes: int, step: int) -> None:
    settings = _settings()
    container = build_container(settings, simulated=minutes > 0)
    await container.startup()
    try:
        runner = PipelineRunner(container)
        await runner.seed()
        if minutes > 0:
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
