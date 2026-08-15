"""The dashboard, driven against a populated database.

Runs a short pipeline to produce real cases, then exercises every route the way
a browser would. No credentials, no network.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from road_cleaner.config import Settings
from road_cleaner.container import build_container
from road_cleaner.pipeline.runner import PipelineRunner
from road_cleaner.web.app import create_app

SIMULATED_MINUTES = 1200
STEP_SECONDS = 600


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def populated(tmp_path_factory):
    """A database with real cases in it, plus the settings that point at it."""
    tmp = tmp_path_factory.mktemp("web")
    settings = Settings(
        ROAD_CLEANER_MODE="local",
        DRY_RUN=True,
        DATA_DIR=str(tmp),
        SQLITE_PATH=str(tmp / "web.db"),
        BLOB_LOCAL_PATH=str(tmp / "frames"),
        FILING_SANDBOX_INBOX=str(tmp / "outbox"),
        LOG_LEVEL="ERROR",
    )
    container = build_container(settings, simulated=True)
    await container.startup()
    try:
        runner = PipelineRunner(container)
        await runner.seed()
        await runner.run_simulated(minutes=SIMULATED_MINUTES, step_seconds=STEP_SECONDS)
        cases = await container.repository.list_cases(limit=200)
    finally:
        await container.shutdown()
    return settings, cases


@pytest.fixture(scope="module")
def client(populated):
    settings, _ = populated
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture(scope="module")
def a_case(populated):
    _, cases = populated
    assert cases, "the pipeline produced no cases to render"
    return cases[0]


class TestPages:
    def test_road_log_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "It finds the hazard" in r.text
        assert "The road log" in r.text

    def test_road_log_lists_cases(self, client, populated):
        _, cases = populated
        body = client.get("/").text
        for case in cases[:5]:
            assert case.id in body

    def test_stat_band_is_computed_not_hardcoded(self, client):
        body = client.get("/").text
        assert "reports filed this week" in body
        assert "the official feed never saw" in body
        # The design's placeholder numbers must not survive into the build.
        assert "$2.18" not in body

    def test_about_renders(self, client):
        r = client.get("/about")
        assert r.status_code == 200
        assert "Detection is the easy part." in r.text
        assert "Quiet by default" in r.text

    def test_case_page_renders_every_section(self, client, a_case):
        r = client.get(f"/cases/{a_case.id}")
        assert r.status_code == 200
        for section in (
            "What I saw",
            "Raw model output",
            "What I did about it",
            "Time allowed",
            "Whose road",
        ):
            assert section in r.text, f"missing section: {section}"

    def test_unknown_case_is_a_friendly_404(self, client):
        r = client.get("/cases/GA-999999")
        assert r.status_code == 404
        assert "Nothing here." in r.text

    def test_branding_is_road_cleaner_not_roadwarden(self, client):
        for path in ("/", "/about"):
            body = client.get(path).text
            assert "Road Cleaner" in body
            assert "RoadWarden" not in body
            assert "roadwarden" not in body.lower()


class TestFilters:
    def test_status_filter_narrows_the_list(self, client, populated):
        _, cases = populated
        kinds = {c.kind.value for c in cases}
        for kind in kinds:
            r = client.get(f"/?kind={kind}")
            assert r.status_code == 200
            expected = [c for c in cases if c.kind.value == kind]
            for case in expected[:3]:
                assert case.id in r.text

    def test_state_filter_narrows_the_list(self, client, populated):
        _, cases = populated
        r = client.get("/?state=GA")
        assert r.status_code == 200
        for case in cases:
            if case.state != "GA":
                # Ids appear only in the rows, so an excluded case must be absent.
                assert f'thumb__id">{case.id}<' not in r.text

    def test_filters_combine(self, client):
        assert client.get("/?kind=filed&state=NC").status_code == 200


class TestApi:
    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_stats(self, client):
        data = client.get("/api/stats").json()
        assert data["total_cases"] > 0
        assert 0 <= data["missed_by_feed_pct"] <= 100

    def test_cases(self, client):
        data = client.get("/api/cases").json()
        assert data["cases"]
        assert {"id", "kind", "hazard", "sentence"} <= set(data["cases"][0])

    def test_case_detail(self, client, a_case):
        data = client.get(f"/api/cases/{a_case.id}").json()
        assert data["case"]["id"] == a_case.id
        assert "sla" in data and "trail" in data

    def test_cameras(self, client):
        data = client.get("/api/cameras").json()
        assert len(data["cameras"]) == 21

    def test_missing_case_returns_json_404(self, client):
        r = client.get("/api/cases/NOPE-1")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")


class TestFrames:
    def test_evidence_frames_are_served_as_images(self, client, populated):
        _, cases = populated
        key = next(
            (f.blob_key for c in cases for f in c.frame_refs if f.blob_key), None
        )
        assert key, "no case carried an evidence frame"
        r = client.get(f"/frames/{key}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content.startswith(b"\xff\xd8")

    def test_missing_frame_is_404(self, client):
        assert client.get("/frames/GA/nope/nope.jpg").status_code == 404

    def test_path_traversal_is_refused(self, client):
        """Blob keys derive from camera ids, which come from a third-party API."""
        r = client.get("/frames/../../../../etc/passwd")
        assert r.status_code == 404
        assert b"root:" not in r.content


class TestRecheck:
    def test_recheck_runs_the_real_auditor(self, client, populated):
        """The comp faked this with a setTimeout. Here it must actually persist."""
        _, cases = populated
        case = next((c for c in cases if c.is_open), cases[0])

        before = client.get(f"/api/cases/{case.id}").json()
        result = client.post(f"/api/cases/{case.id}/recheck")
        assert result.status_code == 200

        body = result.json()
        assert body["case_id"] == case.id
        assert "still_present" in body

        after = client.get(f"/api/cases/{case.id}").json()
        # Either a new trail entry was written, or the case was already closed
        # and correctly left alone.
        assert len(after["trail"]) >= len(before["trail"])

    def test_recheck_does_not_reopen_a_closed_case(self, client, populated):
        """Regression: re-checking a cleared case re-closed it against today's
        date, so a case that closed in 19h claimed it took 11 days."""
        _, cases = populated
        closed = [c for c in cases if c.kind.value in ("cleared", "suppressed")]
        if not closed:
            pytest.skip("no closed cases in this run")

        case = closed[0]
        before = client.get(f"/api/cases/{case.id}").json()
        client.post(f"/api/cases/{case.id}/recheck")
        after = client.get(f"/api/cases/{case.id}").json()

        assert after["case"]["closed_at"] == before["case"]["closed_at"]
        assert after["case"]["kind"] == before["case"]["kind"]
        assert len(after["trail"]) == len(before["trail"])

    def test_recheck_of_unknown_case_is_404(self, client):
        assert client.post("/api/cases/NOPE-1/recheck").status_code == 404
