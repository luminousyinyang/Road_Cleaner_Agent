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


class TestSimulation:
    """The generated-media surface, and the boundary it must not cross."""

    @pytest.fixture(scope="class")
    def with_media(self, populated, a_case):
        """Drop a fake clip and its sidecar into the media store."""
        import json

        settings, _ = populated
        folder = settings.media_local_path / "synthetic" / a_case.id
        folder.mkdir(parents=True, exist_ok=True)
        clip = folder / "2026-08-21T22-07-39-veo-3.1-fast-generate-001.mp4"
        clip.write_bytes(b"\x00\x01" * 4096)
        (folder / (clip.name + ".json")).write_text(
            json.dumps({"model_name": "veo-3.1-fast-generate-001"})
        )
        return f"synthetic/{a_case.id}/{clip.name}", len(clip.read_bytes())

    def test_simulation_page_renders(self, client):
        r = client.get("/simulation")
        assert r.status_code == 200
        assert "Everything on this page is generated" in r.text

    def test_generated_media_is_served_and_badged(self, client, with_media, a_case):
        key, size = with_media
        assert client.get(f"/media/{key}").status_code == 200

        body = client.get(f"/cases/{a_case.id}").text
        assert "<video" in body
        assert "SYNTHETIC — generated by veo-3.1-fast-generate-001" in body

    def test_video_supports_range_requests_so_it_can_seek(self, client, with_media):
        key, size = with_media
        r = client.get(f"/media/{key}", headers={"Range": "bytes=0-1023"})
        assert r.status_code == 206
        assert r.headers["Content-Range"] == f"bytes 0-1023/{size}"
        assert len(r.content) == 1024
        assert r.headers["Accept-Ranges"] == "bytes"

    def test_unsatisfiable_range_falls_back_to_the_whole_body(self, client, with_media):
        key, size = with_media
        r = client.get(f"/media/{key}", headers={"Range": f"bytes={size + 99}-"})
        assert r.status_code == 200
        assert len(r.content) == size

    def test_media_route_refuses_to_serve_camera_evidence(self, client, populated):
        """The boundary. /media serves model output; evidence lives at /frames."""
        _, cases = populated
        frame_key = next(
            (f.blob_key for c in cases for f in c.frame_refs if f.blob_key), None
        )
        assert frame_key, "no evidence frames in the fixture"
        assert client.get(f"/media/{frame_key}").status_code == 404
        assert client.get(f"/frames/{frame_key}").status_code == 200

    def test_generated_media_never_reaches_the_outbox(self, populated):
        """Nothing synthetic may appear in a report that would have been sent."""
        settings, _ = populated
        for report in settings.filing_outbox.glob("*.txt"):
            text = report.read_text().lower()
            assert "synthetic" not in text
            assert ".mp4" not in text


class TestRenderApi:
    """The generate-from-the-UI endpoints, in the default (generation off) mode."""

    def test_generation_is_refused_unless_explicitly_enabled(self, client, a_case):
        """The dashboard must not be able to spend money the config didn't allow."""
        r = client.post(f"/api/simulate/{a_case.id}")
        assert r.status_code == 409
        assert "MEDIA_PROVIDER=vertex" in r.json()["detail"]

    def test_the_button_renders_disabled_when_generation_is_off(self, client, a_case):
        body = client.get(f"/cases/{a_case.id}").text
        assert 'id="gen-run"' in body
        assert "disabled" in body

    def test_the_refusal_comes_before_any_case_lookup(self, client):
        """Generation off means refused, whether or not the case exists.

        Checking config first avoids a pointless database read and avoids
        revealing which case ids exist through a feature that is switched off.
        """
        assert client.post("/api/simulate/NOPE-9999").status_code == 409

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/simulate/jobs/deadbeef").status_code == 404


class TestRenderApiEnabled:
    """With generation switched on -- but never reaching Veo."""

    @pytest.fixture(scope="class")
    def vertex_client(self, populated):
        settings, _ = populated
        enabled = settings.model_copy(update={"media_provider": "vertex"})
        with TestClient(create_app(enabled)) as c:
            yield c

    def test_unknown_case_is_404_not_500(self, vertex_client):
        assert vertex_client.post("/api/simulate/NOPE-9999").status_code == 404

    def test_a_hazard_we_refuse_to_simulate_is_422(self, vertex_client, populated):
        """'Roads, not people' is a rule, not a server error."""
        _, cases = populated
        person = next(
            (c for c in cases if c.hazard_type.value == "pedestrian_on_highway"), None
        )
        if person is None:
            pytest.skip("no pedestrian case in this fixture run")
        r = vertex_client.post(f"/api/simulate/{person.id}")
        assert r.status_code == 422
        assert "person" in r.json()["detail"].lower()

    def test_the_button_is_enabled(self, vertex_client, a_case):
        body = vertex_client.get(f"/cases/{a_case.id}").text
        assert 'id="gen-run"' in body
        assert "bills per second" in body
