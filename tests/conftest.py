"""Shared fixtures.

Everything here runs with zero credentials: temp SQLite, temp blob directory,
in-memory bus, simulated cameras, scripted vision, and a frozen clock. That is
the point -- the test suite proves the "clone it and it works" claim rather than
asserting it in a README.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from road_cleaner.config import Settings
from road_cleaner.container import build_container


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A fully-local configuration pointed at a temp directory."""
    return Settings(
        ROAD_CLEANER_MODE="local",
        DRY_RUN=True,
        DATA_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "test.db"),
        BLOB_LOCAL_PATH=str(tmp_path / "frames"),
        FILING_SANDBOX_INBOX=str(tmp_path / "outbox"),
        LOG_LEVEL="WARNING",
    )


@pytest.fixture
async def container(settings: Settings):
    """A wired container on a frozen clock, started and torn down."""
    c = build_container(settings, simulated=True)
    await c.startup()
    try:
        yield c
    finally:
        await c.shutdown()
