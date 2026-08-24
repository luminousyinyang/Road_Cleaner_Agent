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


def _legacy_schema() -> str:
    """`schema.sql` as it read before the migrated columns were added."""
    import re

    from road_cleaner.adapters.repo.sqlite_repo import _ADDED_COLUMNS

    text = (
        Path(__import__("road_cleaner").__file__).parent
        / "adapters" / "repo" / "schema.sql"
    ).read_text()
    names = {n for cols in _ADDED_COLUMNS.values() for n, _ in cols}
    kept = [ln for ln in text.splitlines()
            if not any(ln.strip().startswith(n + " ") for n in names)]
    # Removing a trailing column leaves "...,\n-- comment\n);" behind.
    return re.sub(r",(\s*(?:--[^\n]*\n\s*)*)\)", r"\1)", "\n".join(kept))


class TestOlderDatabasesStillOpen:
    """`CREATE TABLE IF NOT EXISTS` leaves an existing table alone.

    So a column added after a database was created never appears in it, and
    every read of that table fails until the file is deleted. `data/road_cleaner.db`
    holds the demo week -- deleting it to add one column is not an option, and
    has already gone wrong twice. `_migrate` exists for this; these tests are what
    keep it honest as columns keep being added.
    """

    async def _row_count(self, path: Path, table: str) -> int:
        conn = sqlite3.connect(path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    async def test_a_database_missing_a_new_column_gains_it_without_losing_rows(
        self, tmp_path
    ):
        settings = _settings(tmp_path)
        container = build_container(settings, simulated=True)
        await container.startup()
        runner = PipelineRunner(container)
        await runner.seed()
        await runner.run_simulated(minutes=900, step_seconds=600)
        await container.shutdown()

        path = Path(settings.sqlite_path)
        before = await self._row_count(path, "cases")
        assert before, "fixture run wrote nothing, so this proves nothing"

        # Rebuild the file with the schema as it stood before those columns
        # existed, which is the situation that actually occurs -- rather than
        # DROP COLUMN, which SQLite refuses on a trailing column anyway.
        from road_cleaner.adapters.repo.sqlite_repo import _ADDED_COLUMNS

        rows = {}
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        for table in _ADDED_COLUMNS:
            rows[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        conn.close()
        path.unlink()
        for sidecar in (path.with_suffix(".db-wal"), path.with_suffix(".db-shm")):
            sidecar.unlink(missing_ok=True)

        conn = sqlite3.connect(path)
        conn.executescript(_legacy_schema())
        for table, saved in rows.items():
            for row in saved:
                keep = {k: v for k, v in row.items()
                        if k not in {n for n, _ in _ADDED_COLUMNS[table]}}
                conn.execute(
                    f"INSERT INTO {table} ({','.join(keep)}) "
                    f"VALUES ({','.join('?' * len(keep))})",
                    tuple(keep.values()),
                )
        conn.commit()
        conn.close()

        # Reopening must migrate rather than fail, and must not touch the data.
        repo = SqliteCaseRepository(path)
        await repo.initialize()
        try:
            cases = await repo.list_cases()
            assert len(cases) == before
        finally:
            await repo.close()

    async def test_migrating_twice_is_a_no_op(self, tmp_path):
        """Startup runs it every time; the second run must not raise."""
        path = tmp_path / "twice.db"
        for _ in range(2):
            repo = SqliteCaseRepository(path)
            await repo.initialize()
            await repo.close()


class TestReSavingACase:
    """A case's description has to be able to change without contradicting itself."""

    async def test_the_hazard_type_and_its_title_move_together(self, tmp_path):
        """Regression: the upsert updated the title but not the type.

        A case re-saved after being re-derived from its own footage came back
        titled "Debris in a travel lane" while still typed `animal`, so the
        headline and the badge on the same page disagreed about what it was.
        """
        from road_cleaner.domain.enums import HazardType
        from road_cleaner.domain.models import Case

        repo = SqliteCaseRepository(tmp_path / "retype.db")
        await repo.initialize()
        try:
            case = Case(
                id="GA-0001", camera_id="c", state="GA",
                hazard_type=HazardType.ANIMAL, hazard_title="Animal near the carriageway",
                location="I-285 at Camp Creek Pkwy",
            )
            await repo.save_case(case)

            case.hazard_type = HazardType.DEBRIS
            case.hazard_title = "Debris in a travel lane"
            await repo.save_case(case)

            stored = await repo.get_case("GA-0001")
            assert stored.hazard_type is HazardType.DEBRIS
            assert stored.hazard_title == "Debris in a travel lane"
        finally:
            await repo.close()


class TestACaseCanMove:
    """A case's location was fixed when it opened, from whatever camera saw it.

    That is right for a camera and wrong for a re-staged clip, which could have
    happened anywhere. The upsert did not carry `location` or `state`, so a moved
    case reported its new home and then read as the old one on reload — the same
    omission as `hazard_type`, found the same way.
    """

    async def test_location_and_state_survive_a_re_save(self, tmp_path):
        from road_cleaner.domain.enums import HazardType
        from road_cleaner.domain.models import Case

        repo = SqliteCaseRepository(tmp_path / "moved.db")
        await repo.initialize()
        try:
            case = Case(
                id="GA-0001", camera_id="c", state="GA", hazard_type=HazardType.DEBRIS,
                hazard_title="Debris in a travel lane",
                location="I-285 westbound at Camp Creek Pkwy",
            )
            await repo.save_case(case)

            case.location = "39.96120, -82.99880 — near Columbus, OH"
            case.state = "OH"
            await repo.save_case(case)

            stored = await repo.get_case("GA-0001")
            assert stored.location.endswith("near Columbus, OH")
            assert stored.state == "OH"
        finally:
            await repo.close()


class TestEveryFieldSurvivesAReSave:
    """The bug that kept coming back, closed structurally.

    `save_case` upserts, and its `DO UPDATE SET` clause used to be written out by
    hand. Three times a column was added to the insert and forgotten in the
    update, and each time it surfaced as something that looked unrelated:

    * a case re-titled "Debris in a travel lane" while still typed `animal`;
    * a box label showing 0.77 against a case confidence of 0.91;
    * a case moved to Ohio that read as Georgia again on reload.

    The clause is derived from the column list now. These tests are what keep
    the column list itself honest.
    """

    def test_every_column_is_updated_except_the_ones_we_chose_not_to(self):
        from road_cleaner.adapters.repo.sqlite_repo import (
            _CASE_COLUMNS,
            _CASE_IMMUTABLE,
            _CASE_UPSERT,
        )

        for column in _CASE_COLUMNS:
            present = f"{column}=excluded.{column}" in _CASE_UPSERT
            if column in _CASE_IMMUTABLE:
                assert not present, f"{column} is declared immutable but is updated"
            else:
                assert present, f"{column} is written on insert and never updated"

    def test_the_immutable_columns_are_the_ones_we_meant(self):
        """`id` is identity. `opened_at` is when this case began — a fact about
        the past, not a field to refresh."""
        from road_cleaner.adapters.repo.sqlite_repo import _CASE_IMMUTABLE

        assert {"id", "opened_at"} == _CASE_IMMUTABLE

    def test_the_column_list_matches_the_model(self):
        """A field added to `Case` and forgotten here would never persist."""
        from road_cleaner.adapters.repo.sqlite_repo import _CASE_COLUMNS
        from road_cleaner.domain.models import Case

        assert set(Case.model_fields) == set(_CASE_COLUMNS)

    def test_the_column_list_matches_the_schema(self):
        """And a column in the table that nothing writes is dead weight.

        `correlation_key` is the deliberate exception: it is not a field on the
        model and `set_correlation` owns it.
        """
        import re

        from road_cleaner.adapters.repo.sqlite_repo import _CASE_COLUMNS, SCHEMA_PATH

        block = re.search(
            r"CREATE TABLE IF NOT EXISTS cases \((.*?)\n\);", SCHEMA_PATH.read_text(), re.S
        ).group(1)
        columns = {
            line.strip().split()[0]
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("--")
        }
        assert columns - set(_CASE_COLUMNS) == {"correlation_key"}

    async def test_a_changed_case_round_trips_field_by_field(self, tmp_path):
        """The real proof: mutate everything mutable, save, reopen, compare."""
        from datetime import UTC, datetime

        from road_cleaner.domain.enums import CaseKind, Channel, GateDecision, HazardType, Severity
        from road_cleaner.domain.models import BoundingBox, Case, FrameRef

        repo = SqliteCaseRepository(tmp_path / "roundtrip.db")
        await repo.initialize()
        try:
            opened = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
            await repo.save_case(
                Case(
                    id="GA-0001", camera_id="cam-a", state="GA",
                    hazard_type=HazardType.ANIMAL, hazard_title="Animal on the shoulder",
                    location="I-285 at Camp Creek Pkwy", opened_at=opened,
                )
            )

            changed = Case(
                id="GA-0001", camera_id="cam-b", state="OH",
                kind=CaseKind.ESCALATED, synthetic=True,
                hazard_type=HazardType.DEBRIS, hazard_title="Debris in a travel lane",
                location="39.96120, -82.99880 — near Columbus, OH",
                severity=Severity.CRITICAL, confidence=0.91,
                opened_at=opened,
                updated_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
                closed_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
                gate_decision=GateDecision.SUPPRESS, gate_reason="already posted",
                agency_id="oh-dot", agency_name="Ohio DOT", channel=Channel.EMAIL,
                reference="RC-OH-12345", ref_label="OHIO DOT",
                sla_deadline=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
                escalation_tier=2,
                last_checked_at=datetime(2026, 8, 6, 6, 0, tzinfo=UTC),
                next_check_at=datetime(2026, 8, 8, 6, 0, tzinfo=UTC),
                checks_done=7, sentence="a new sentence", explain="a new explanation",
                detection_ids=["d1", "d2"],
                frame_refs=[FrameRef(label="First sighting", blob_key="k.jpg", mark=True)],
                raw_model_json='{"source":"reinspect"}',
                box=BoundingBox(x=0.1, y=0.2, width=0.3, height=0.4),
                box_label="debris · 0.91",
            )
            await repo.save_case(changed)

            stored = await repo.get_case("GA-0001")
            for field in Case.model_fields:
                if field == "opened_at":
                    assert stored.opened_at == opened, "opened_at must not be rewritten"
                    continue
                assert getattr(stored, field) == getattr(changed, field), (
                    f"{field} did not survive a re-save"
                )
        finally:
            await repo.close()
