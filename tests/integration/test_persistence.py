"""Data has to survive the process that wrote it.

The pipeline and the dashboard are separate processes. `road-cleaner demo`
populates the store and exits; `road-cleaner serve` starts fresh and reads it.
If committed writes are still sitting in the WAL sidecar when the first process
goes away, the dashboard comes up nearly empty and the run looks like it did
almost nothing.

That happened: a run reporting 11 cases left 2 readable. Hence these tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from road_cleaner.adapters.repo.sqlite_repo import SqliteCaseRepository
from road_cleaner.config import Settings
from road_cleaner.container import build_container
from road_cleaner.pipeline.runner import PipelineRunner


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        ROAD_CLEANER_MODE="local",
        DRY_RUN=True,
        DATA_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "persist.db"),
        BLOB_LOCAL_PATH=str(tmp_path / "frames"),
        FILING_SANDBOX_INBOX=str(tmp_path / "outbox"),
        LOG_LEVEL="ERROR",
    )


class TestDurability:
    async def test_cases_survive_closing_the_repository(self, tmp_path):
        """The core guarantee: write, close, reopen, everything is still there."""
        settings = _settings(tmp_path)

        container = build_container(settings, simulated=True)
        await container.startup()
        runner = PipelineRunner(container)
        await runner.seed()
        await runner.run_simulated(minutes=900, step_seconds=600)
        written = await container.repository.list_cases(limit=500)
        await container.shutdown()

        assert written, "the run produced no cases to persist"

        # A brand new repository object over the same file, as a second process
        # would see it.
        reopened = SqliteCaseRepository(Path(settings.sqlite_path))
        await reopened.initialize()
        try:
            read_back = await reopened.list_cases(limit=500)
            assert len(read_back) == len(written)
            assert {c.id for c in read_back} == {c.id for c in written}

            # And the related rows, not just the case headers.
            for case in read_back:
                assert await reopened.get_trail(case.id), f"{case.id} lost its trail"
        finally:
            await reopened.close()

    async def test_the_wal_is_checkpointed_on_close(self, tmp_path):
        """After a clean shutdown the database file is self-contained.

        A leftover -wal next to the main file is the mechanism by which the
        data went missing, so this asserts on the artefact directly.
        """
        settings = _settings(tmp_path)
        container = build_container(settings, simulated=True)
        await container.startup()
        runner = PipelineRunner(container)
        await runner.seed()
        await runner.run_simulated(minutes=600, step_seconds=600)
        await container.shutdown()

        db = Path(settings.sqlite_path)
        wal = Path(f"{db}-wal")
        assert db.exists()
        # TRUNCATE leaves the WAL either gone or empty.
        assert not wal.exists() or wal.stat().st_size == 0

        # Readable by a plain sqlite3 client with no recovery step.
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0] == 21
        finally:
            conn.close()

    async def test_evidence_frames_outlive_the_run(self, tmp_path):
        settings = _settings(tmp_path)
        container = build_container(settings, simulated=True)
        await container.startup()
        runner = PipelineRunner(container)
        await runner.seed()
        await runner.run_simulated(minutes=900, step_seconds=600)
        cases = await container.repository.list_cases(limit=500)
        await container.shutdown()

        keys = [f.blob_key for c in cases for f in c.frame_refs if f.blob_key]
        assert keys, "no evidence frames were recorded"
        for key in keys:
            path = Path(settings.blob_local_path) / key
            assert path.exists(), f"evidence frame missing from disk: {key}"
            assert path.read_bytes().startswith(b"\xff\xd8")


class TestReset:
    def test_reset_survives_a_nested_frame_tree(self, tmp_path):
        """Regression: rmtree raised 'Directory not empty' on the nested
        state/camera/ layout and aborted the run before a database existed."""
        from road_cleaner.cli import _reset_state

        settings = _settings(tmp_path)
        settings.ensure_directories()

        nested = Path(settings.blob_local_path) / "GA" / "GDOT-CCTV-0447"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "frame.jpg").write_bytes(b"\xff\xd8stub")
        Path(settings.sqlite_path).write_bytes(b"")
        Path(f"{settings.sqlite_path}-wal").write_bytes(b"")

        _reset_state(settings)

        assert not Path(settings.sqlite_path).exists()
        assert not Path(f"{settings.sqlite_path}-wal").exists()
        assert not (Path(settings.blob_local_path) / "GA").exists()
        # And the directories the next run needs are back.
        assert Path(settings.blob_local_path).is_dir()
        assert Path(settings.filing_outbox).is_dir()

    def test_reset_is_safe_to_run_twice(self, tmp_path):
        from road_cleaner.cli import _reset_state

        settings = _settings(tmp_path)
        _reset_state(settings)
        _reset_state(settings)  # must not raise on already-clean state

    @pytest.mark.parametrize("missing", ["db", "frames", "outbox"])
    def test_reset_tolerates_missing_pieces(self, tmp_path, missing):
        from road_cleaner.cli import _reset_state

        settings = _settings(tmp_path)
        settings.ensure_directories()
        if missing == "frames":
            Path(settings.blob_local_path).rmdir()
        elif missing == "outbox":
            Path(settings.filing_outbox).rmdir()
        _reset_state(settings)
