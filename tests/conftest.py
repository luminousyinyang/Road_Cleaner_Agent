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


@pytest.fixture(autouse=True, scope="session")
def _ignore_the_developers_env() -> None:
    """Stop `Settings` reading `.env` while the suite runs.

    The claim at the top of this file -- zero credentials, scripted vision, no
    network -- stopped being true the moment somebody configured Vertex locally,
    because `Settings()` reads `.env` and every test that builds one inherited
    it. Two ways that showed up, both of them silent:

    * `test_media_generation_is_off_by_default_even_in_cloud_mode` asserts a
      default, and a local `MEDIA_PROVIDER=vertex` made it assert the developer's
      configuration instead.
    * `test_a_drill_runs_the_whole_pipeline_and_stops` started making real
      Gemini calls and failing on `429 RESOURCE_EXHAUSTED` -- a green suite on
      one machine and a rate-limited one on another, for a test that is supposed
      to touch nothing.

    Session-scoped and autouse because there is no case where a test should read
    it. Real environment variables still apply, which is how CI overrides things.
    """
    Settings.model_config["env_file"] = None


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
