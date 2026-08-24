"""The boundary between drill cases and real ones.

A drill invents a location, generates its own footage, and runs the real
pipeline over it. That is useful and honest right up until one of those cases
looks like a real filed report — so every guarantee below is asserted rather
than assumed:

* a drill case can never be filed,
* it never appears in the road log or the public statistics,
* the Auditor never tries to re-check it,
* and nothing about it reaches the outbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from road_cleaner.adapters.repo.sqlite_repo import SqliteCaseRepository
from road_cleaner.agents.dispatcher import SyntheticCaseError
from road_cleaner.domain.enums import CaseKind, GateDecision, HazardType, Severity
from road_cleaner.domain.models import Camera, Case, Detection


def _case(case_id: str, *, synthetic: bool, kind: CaseKind = CaseKind.FILED) -> Case:
    return Case(
        id=case_id,
        camera_id="CAM-1",
        state=case_id.split("-")[0],
        kind=kind,
        hazard_type=HazardType.DEBRIS,
        hazard_title="Debris in lane 1",
        location="I-75 northbound",
        severity=Severity.MEDIUM,
        confidence=0.9,
        gate_decision=GateDecision.FILE,
        synthetic=synthetic,
    )


@pytest.fixture
async def repo(tmp_path: Path):
    r = SqliteCaseRepository(tmp_path / "t.db")
    await r.initialize()
    try:
        yield r
    finally:
        await r.close()


# ------------------------------------------------------------------ storage


async def test_the_flag_survives_a_round_trip(repo):
    await repo.save_case(_case("SIM-1", synthetic=True))
    await repo.save_case(_case("GA-1", synthetic=False))

    assert (await repo.get_case("SIM-1")).synthetic is True
    assert (await repo.get_case("GA-1")).synthetic is False


async def test_drill_cases_are_excluded_from_the_road_log_by_default(repo):
    """Exclusion is the default so no caller has to remember to filter."""
    await repo.save_case(_case("SIM-1", synthetic=True))
    await repo.save_case(_case("GA-1", synthetic=False))

    listed = await repo.list_cases(limit=50)
    assert [c.id for c in listed] == ["GA-1"]

    both = await repo.list_cases(limit=50, include_synthetic=True)
    assert {c.id for c in both} == {"SIM-1", "GA-1"}


async def test_the_auditor_never_sees_a_drill_case(repo):
    """It would go looking for a camera that does not exist."""
    await repo.save_case(_case("SIM-1", synthetic=True, kind=CaseKind.WATCHING))
    await repo.save_case(_case("GA-1", synthetic=False, kind=CaseKind.WATCHING))

    assert [c.id for c in await repo.open_cases()] == ["GA-1"]


async def test_drill_cases_do_not_count_in_public_statistics(repo):
    """Every number on the dashboard is a claim about a real road."""
    await repo.save_case(_case("GA-1", synthetic=False))
    before = await repo.stats(datetime.now(UTC))

    for i in range(5):
        await repo.save_case(_case(f"SIM-{i}", synthetic=True))
    after = await repo.stats(datetime.now(UTC))

    assert after["total_cases"] == before["total_cases"]
    assert after["missed_by_feed_pct"] == before["missed_by_feed_pct"]


async def test_an_old_database_gains_the_column(tmp_path: Path):
    """`CREATE TABLE IF NOT EXISTS` will not add a column to an existing table."""
    import sqlite3

    path = tmp_path / "old.db"
    schema = (
        Path("src/road_cleaner/adapters/repo/schema.sql").read_text().replace(
            ",\n    -- Drill cases: invented location, generated footage, real pipeline. Never\n"
            "    -- filed, never counted in the public statistics. See domain.models.Case.\n"
            "    synthetic       INTEGER NOT NULL DEFAULT 0",
            "",
        )
    )
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    assert "synthetic" not in schema.split("CREATE TABLE IF NOT EXISTS cases")[1][:900]

    repo = SqliteCaseRepository(path)
    await repo.initialize()
    try:
        await repo.save_case(_case("GA-1", synthetic=False))
        assert (await repo.get_case("GA-1")).synthetic is False
    finally:
        await repo.close()


# ------------------------------------------------------------------- filing


async def test_filing_a_drill_case_raises(container):
    """The guarantee that matters. A silent skip would be indistinguishable
    from 'nothing to file' and could go unnoticed for a long time."""
    from road_cleaner.agents.dispatcher import Dispatcher

    dispatcher = Dispatcher(container)
    case = _case("SIM-1", synthetic=True)
    camera = Camera(
        id="CAM-1", state="GA", name="somewhere", road="I-75",
        lat=33.8, lng=-84.4, snapshot_url="sim://x",
    )
    detection = Detection(
        camera_id="CAM-1", frame_id="f1", hazard_type=HazardType.DEBRIS,
        lane_position="lane_1", severity=Severity.MEDIUM, confidence=0.9,
        description="debris",
    )

    with pytest.raises(SyntheticCaseError, match="never be filed"):
        await dispatcher.file_case(case, camera, detection)


async def test_a_drill_case_writes_nothing_to_the_outbox(container, settings):
    from road_cleaner.agents.dispatcher import Dispatcher

    outbox = Path(settings.filing_outbox)
    before = set(outbox.glob("*")) if outbox.exists() else set()

    case = _case("SIM-1", synthetic=True)
    camera = Camera(
        id="CAM-1", state="GA", name="somewhere", road="I-75",
        lat=33.8, lng=-84.4, snapshot_url="sim://x",
    )
    detection = Detection(
        camera_id="CAM-1", frame_id="f1", hazard_type=HazardType.DEBRIS,
        lane_position="lane_1", severity=Severity.MEDIUM, confidence=0.9,
        description="debris",
    )
    with pytest.raises(SyntheticCaseError):
        await Dispatcher(container).file_case(case, camera, detection)

    after = set(outbox.glob("*")) if outbox.exists() else set()
    assert after == before
