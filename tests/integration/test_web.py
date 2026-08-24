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
    def test_the_front_door_leads_with_the_agent(self, client):
        """`/` opens with what the agent does, not with the simulation.

        Judges score autonomous action that removes friction. "It finds the
        hazard, then files the paperwork" answers that; leading with training
        data does not.
        """
        r = client.get("/")
        assert r.status_code == 200
        assert "files the" in r.text and "paperwork" in r.text
        # ...and the drill, which is how you watch it happen.
        assert "drill-form" in r.text
        # ...with the simulation still present, below.
        assert "scenario library" in r.text.lower()

    def test_road_log_lists_cases(self, client, populated):
        _, cases = populated
        body = client.get("/").text
        for case in cases[:5]:
            assert case.id in body

    def test_stat_band_is_computed_not_hardcoded(self, client):
        """Kept on the new front page: the proof the detections are real."""
        body = client.get("/").text
        assert "reports filed" in body
        assert "median spot" in body
        # The design's placeholder numbers must not survive into the build.
        assert "$2.18" not in body

    def test_no_stat_is_labelled_as_something_it_does_not_measure(self, client):
        """Two of these were.

        "the official feed never saw" reads as a comparison against a state 511
        feed; it is really the gate's file-vs-suppress ratio, duplicates and all.
        "reports filed this week" was measured back from the newest filing, so a
        database untouched for a month still claimed a week's work.
        """
        body = client.get("/").text
        assert "the official feed never saw" not in body
        assert "reports filed this week" not in body

    def test_about_renders(self, client):
        r = client.get("/about")
        assert r.status_code == 200
        assert "Detection is the easy part." in r.text
        assert "Quiet by default" in r.text

    def test_case_page_renders_every_section(self, client, a_case):
        """What is left after the sidebar went.

        "Time allowed" and "Whose road" were panels restating what the run says;
        the agency now appears on the one-line facts row instead.
        """
        r = client.get(f"/cases/{a_case.id}")
        assert r.status_code == 200
        assert "Raw model output" in r.text

    def test_the_step_by_step_trail_is_not_rendered(self, client, a_case):
        """It was a long list nobody opened.

        What could only be learned inside it -- a filing that failed, or the
        decision to stop escalating -- is lifted onto the facts row instead, so
        trouble is visible without opening anything. The data stays on the API.
        """
        body = client.get(f"/cases/{a_case.id}").text
        assert "What I did about it" not in body
        assert 'class="trail"' not in body
        assert "trail" in client.get(f"/api/cases/{a_case.id}").json()

    def test_the_report_is_not_printed_twice(self, client, populated):
        """The sidebar carried a second copy of the same report body.

        Two identical walls of text on one page was most of why it read as long.
        """
        _, cases = populated
        filed = next((c for c in cases if c.was_filed and c.reference), None)
        if filed is None:
            pytest.skip("no filed case in the fixture")
        body = client.get(f"/cases/{filed.id}").text
        # The cached analysis rides along in a data- attribute, which is not
        # something a reader sees. Count what is rendered.
        import re

        rendered = re.sub(r"data-analysis='[^']*'", "", body)
        assert rendered.count("Filed automatically by Road Cleaner.") <= 1

    def test_the_page_shows_the_run_rather_than_describing_it(self, client, a_case):
        """The prose block this replaced explained what the agent does.

        The demo does it instead -- so the explanatory copy is gone on purpose,
        and its return would mean the page had drifted back to narrating itself.
        """
        body = client.get(f"/cases/{a_case.id}").text
        assert "What I saw" not in body
        assert "Run the agent on this clip" in body or "run__missing" in body

    def test_the_send_button_is_present_and_says_it_will_not_send(
        self, client, a_case
    ):
        """The restraint has to read as a decision, not a missing feature."""
        body = client.get(f"/cases/{a_case.id}").text
        if "run__missing" in body:
            pytest.skip("this case has no clip to run against")
        assert "run-send" in body

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

    def test_retired_pages_redirect_to_the_library(self, client):
        """/log and /simulation were in the README, so they stay resolvable."""
        for path in ("/log", "/simulation"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 301, path
            assert r.headers["location"] == "/"

    def test_the_homepage_shows_no_fixture_camera_renders(self, client, with_media):
        """The camera frames in this build are Pillow-drawn placeholders. They
        read as wireframe mockups next to real-looking generated footage, so the
        page shows the clips and links the frames rather than displaying them."""
        body = client.get("/").text
        assert '<img src="/frames' not in body
        # ...but the clips are there.
        assert "/media/synthetic" in body

    def test_the_generated_boundary_is_stated_on_the_page(self, client):
        """However the copy is reworded, the page must still say plainly that the
        clips are generated and that the civic record contains none of them."""
        # Collapsed whitespace: the assertions are about what the page says, not
        # about where the template happens to wrap its lines.
        body = " ".join(client.get("/").text.split())
        assert "not one frame of it was filmed" in body
        assert "contain no generated media" in body

    def test_generated_media_is_served_and_badged(self, client, with_media, a_case):
        key, size = with_media
        assert client.get(f"/media/{key}").status_code == 200

        body = client.get(f"/cases/{a_case.id}").text
        assert "<video" in body
        # The badge may be the long or the short form depending on how much room
        # the surface has. What must never vary is that it says SYNTHETIC and
        # names the model that produced the clip.
        assert "SYNTHETIC" in body
        assert "veo-3.1-fast-generate-001" in body

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

    def test_the_button_renders_disabled_when_generation_is_off(self, client):
        """Generation lives in the library now, not on the case page."""
        body = client.get("/").text
        assert "gen-run" in body
        assert "disabled" in body
        assert "MEDIA_PROVIDER=vertex" in body

    def test_the_case_page_has_no_generate_control(self, client, a_case):
        assert "gen-run" not in client.get(f"/cases/{a_case.id}").text

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

    def test_the_button_is_enabled(self, vertex_client):
        body = vertex_client.get("/").text
        assert "gen-run" in body
        assert "bills per second" in body


class TestDryRunIsToldHonestly:
    """A composed-but-unsent report must not read as a filed one.

    The case page used to show a green card headed "How it went out", carrying a
    reference like `MR-68191` — a number this system minted itself by hashing the
    case id into NCDOT's format. Nothing had gone out and no agency had issued
    that number. The DRY RUN disclosure was a thousand pixels further down.
    """

    def test_the_page_never_claims_an_unsent_report_went_out(self, client, populated):
        _, cases = populated
        filed = next((c for c in cases if c.reference), None)
        assert filed, "no filed case in the fixture"
        body = client.get(f"/cases/{filed.id}").text

        assert "How it went out" not in body
        assert "message that went out" not in body
        # The sidebar that used to carry this is gone; the conditional tense
        # survives on the one-line facts row.
        assert "would go out by" in body.lower()

    def test_a_locally_minted_reference_is_not_shown_at_all(self, client, populated):
        _, cases = populated
        filed = next((c for c in cases if c.reference), None)
        body = client.get(f"/cases/{filed.id}").text

        # Stronger than the caveat it replaces. `synthesize_reference` hashes the
        # case id into the agency's own format, so it is indistinguishable from a
        # real GDOT or NCDOT number -- and no agency has issued it. It used to be
        # presented with a sentence explaining that; not presenting it says the
        # same thing in no words at all.
        #
        # Scoped to the header, not the whole page: the audit trail records that
        # the system minted this reference, and that is a true account of what
        # happened. Editing history to tidy a demo is the opposite of the point.
        import re

        facts = re.search(r'<p class="facts">(.*?)</p>', body, re.S)
        assert facts, "the facts row is missing"
        assert filed.reference not in facts.group(1), "a fabricated reference is presented as fact"
        assert "placeholder" not in facts.group(1).lower()

    def test_the_api_carries_the_same_caveat(self, client, populated):
        """Not just the template -- /api/cases/{id} is documented surface."""
        _, cases = populated
        filed = next((c for c in cases if c.reference), None)
        data = client.get(f"/api/cases/{filed.id}").json()

        assert data["was_dry_run"] is True
        assert data["reference_is_placeholder"] is True
        assert data["channel_label"] == "How it would go out"

    def test_hazards_we_refuse_to_simulate_stay_out_of_the_gallery(self, client, populated):
        """...but the case itself survives: it is a real detection, and it still
        counts in the statistics."""
        _, cases = populated
        person = next(
            (c for c in cases if c.hazard_type.value == "pedestrian_on_highway"), None
        )
        if person is None:
            pytest.skip("no pedestrian case in this fixture run")

        assert person.id not in client.get("/").text
        assert client.get(f"/cases/{person.id}").status_code == 200


class TestCheckNowSaysWhatHappened:
    """The button ran the Auditor and then told the user nothing.

    It wrote its result into the audit trail a thousand pixels down the page, and
    when the Auditor legitimately found nothing worth recording it returned
    `trail_entry: null` and the UI rendered an empty string. From the user's seat
    the button was broken. It was not — it was silent.
    """

    def test_a_closed_case_explains_why_it_will_not_run(self, client, populated):
        _, cases = populated
        closed = next((c for c in cases if not c.is_open), None)
        if closed is None:
            pytest.skip("no closed case in this fixture run")

        body = client.post(f"/api/cases/{closed.id}/recheck").json()
        assert body["ran"] is False
        assert "not re-checked" in body["message"]

    def test_an_open_case_always_returns_a_message(self, client, populated):
        _, cases = populated
        open_case = next((c for c in cases if c.is_open), None)
        if open_case is None:
            pytest.skip("no open case in this fixture run")

        body = client.post(f"/api/cases/{open_case.id}/recheck").json()
        assert body["ran"] is True
        # Whether or not anything was written to the trail.
        assert body["message"], "the endpoint must always say what it found"

    def test_the_page_has_somewhere_to_show_it(self, client, a_case):
        assert 'id="recheck-said"' in client.get(f"/cases/{a_case.id}").text


class TestProvenanceIsStated:
    """Whether a model actually looked at the frame, said on the page.

    The narrative under "What I saw" is generated by `narrative.explain` from the
    detection, and it reads identically whether that detection came from Gemini
    or was replayed out of `seeds/scenarios.json`. Without saying which, the page
    presents canned demo data in the exact voice of a live model call.
    """

    def test_a_scripted_case_says_so(self, client, populated):
        """The paragraph became a chip, but the claim has to survive.

        A page that narrates a replayed detection in the voice of a live model
        call is the thing this guards against, however short the wording gets.
        """
        _, cases = populated
        body = client.get(f"/cases/{cases[0].id}").text
        # The fixture pipeline runs the scripted analyzer.
        assert "replayed" in body
        assert "seeds/scenarios.json" in body
        assert "gemini" not in body.lower()

    def test_the_api_exposes_the_same_flag(self, client, populated):
        _, cases = populated
        data = client.get(f"/api/cases/{cases[0].id}").json()
        assert data["is_scripted"] is True
        assert data["model_name"] == "scripted"

    def test_the_flag_follows_the_detection_not_the_config(self, populated):
        """A database can hold scripted cases from one run and real ones from
        another, so this must be decided per case."""
        from road_cleaner.web.serializers import case_detail

        settings, cases = populated
        # A case whose detection names a real model must not be called scripted.
        from datetime import UTC, datetime

        from road_cleaner.domain.models import CaseWithDetail, Detection

        det = Detection(
            camera_id=cases[0].camera_id, frame_id="f", hazard_type=cases[0].hazard_type,
            lane_position="lane_1", severity=cases[0].severity, confidence=0.9,
            description="x", model_name="gemini-3.7-flash",
        )
        view = case_detail(
            CaseWithDetail(case=cases[0], detections=[det]), datetime.now(UTC)
        )
        assert view["is_scripted"] is False
        assert view["model_name"] == "gemini-3.7-flash"

    def test_a_later_recheck_does_not_rewrite_who_found_the_hazard(self, populated):
        """Reading `detections[-1]` made six of eleven pages disown Gemini.

        The Auditor appends a scripted clearance check to a case days after it
        opened. That check is evidence about whether the road is clear now -- it
        says nothing about who spotted the hazard, and letting it set the
        provenance banner made real detections claim to be canned demo data.
        """
        from datetime import UTC, datetime, timedelta

        from road_cleaner.domain.models import CaseWithDetail, Detection
        from road_cleaner.web.serializers import case_detail

        _, cases = populated
        case = cases[0]
        common = dict(
            camera_id=case.camera_id, frame_id="f", hazard_type=case.hazard_type,
            lane_position="lane_1", severity=case.severity, description="x",
        )
        opened = Detection(
            **common, confidence=0.91, model_name="gemini-3.7-flash",
            analyzed_at=datetime.now(UTC),
        )
        recheck = Detection(
            **common, confidence=0.40, model_name="scripted",
            analyzed_at=datetime.now(UTC) + timedelta(days=2),
        )

        view = case_detail(CaseWithDetail(case=case, detections=[opened, recheck]),
                           datetime.now(UTC))
        assert view["model_name"] == "gemini-3.7-flash"
        assert view["is_scripted"] is False


class TestTheOverlayCaptionIsNotStale:
    """`cases.box_label` is written once at open and then drifts.

    NC-1169 stores 0.77 in that column against a case confidence of 0.91, so a
    page rendering the column shows a number the rest of the same page
    contradicts. The caption is derived instead.
    """

    def test_a_drifted_column_is_ignored_in_favour_of_the_detection(self, populated):
        from datetime import UTC, datetime

        from road_cleaner.domain.models import CaseWithDetail, Detection
        from road_cleaner.web.serializers import case_detail

        _, cases = populated
        case = cases[0].model_copy(update={"box_label": "debris · 0.77", "confidence": 0.91})
        det = Detection(
            camera_id=case.camera_id, frame_id="f", hazard_type=case.hazard_type,
            lane_position="lane_1", severity=case.severity, confidence=0.91,
            description="x", model_name="gemini-3.7-flash",
        )
        view = case_detail(CaseWithDetail(case=case, detections=[det]), datetime.now(UTC))
        assert "0.77" not in view["box_label"]
        assert "0.91" in view["box_label"]

    def test_the_log_row_caption_agrees_with_its_own_confidence(self, populated):
        from road_cleaner.web.serializers import case_row

        _, cases = populated
        case = cases[0].model_copy(update={"box_label": "debris · 0.10", "confidence": 0.88})
        assert case_row(case)["box_label"].endswith("0.88")


class TestInspectRoutes:
    """Starting and polling a live analysis of a case's clip.

    The analysis itself is covered in `tests/unit/test_inspect.py`. What is
    worth testing at this level is the contract the page depends on: a job id
    comes back immediately, polling it returns progress, an unknown case is a
    404 rather than a background crash, and two clicks do not become two runs.
    """

    def test_starting_an_analysis_returns_a_job_to_poll(self, client, a_case):
        response = client.post(f"/api/cases/{a_case.id}/inspect")
        assert response.status_code == 202
        job = response.json()
        assert job["id"] and job["case_id"] == a_case.id
        assert job["state"] in {"running", "done", "failed"}

        polled = client.get(f"/api/inspect/{job['id']}")
        assert polled.status_code == 200
        assert polled.json()["id"] == job["id"]

    def test_an_unknown_case_is_refused_before_any_work_starts(self, client):
        response = client.post("/api/cases/NOPE-1/inspect")
        assert response.status_code == 404

    def test_an_unknown_job_is_a_404(self, client):
        assert client.get("/api/inspect/deadbeef").status_code == 404

    def test_two_clicks_share_one_run(self, client, a_case):
        """Each run spends Vertex quota, which is the scarce resource here."""
        first = client.post(f"/api/cases/{a_case.id}/inspect").json()
        second = client.post(f"/api/cases/{a_case.id}/inspect").json()
        # Either the first is still going and is handed back, or it finished
        # between the two calls and a fresh one legitimately starts.
        if second["state"] == "running" and first["state"] == "running":
            assert second["id"] == first["id"]

    def test_a_clip_problem_fails_the_job_rather_than_the_request(
        self, client, a_case
    ):
        """The route only knows the case exists.

        Whether it has usable footage is discovered by the run, so a missing or
        undecodable clip has to surface as a job error the page can show --
        never as an unhandled exception, and never as a job stuck reporting
        "running" that the page would poll forever. The message has to say what
        to do about it, because it is shown to a person, not to a log.
        """
        import time

        job = client.post(f"/api/cases/{a_case.id}/inspect").json()
        for _ in range(80):
            polled = client.get(f"/api/inspect/{job['id']}").json()
            if polled["state"] != "running":
                break
            time.sleep(0.1)

        assert polled["state"] == "failed", "the job never resolved"
        error = polled["error"] or ""
        # Either there is no clip, or there is one and it will not decode.
        assert "clip" in error or "not a readable video" in error, error
        assert "ffmpeg" not in error.lower(), "names the library, not the problem"


class TestGeneratedMediaIsTypedCorrectly:
    """`/media` serves whatever a model produced, which is no longer just video.

    An analysis leaves a boxed still behind, and it went out as
    application/octet-stream until this was noticed -- it rendered anyway,
    because browsers sniff image bytes, which is exactly the kind of thing that
    works until the day it does not.
    """

    def _put(self, client, key: str, data: bytes) -> None:
        """Write a blob from a sync test.

        `client` is the sync TestClient, so there is no running loop to await
        on -- `asyncio.run` is the whole trick.
        """
        import asyncio

        blobs = client.app.state.container.media_blobs
        asyncio.run(blobs.put(key, data, content_type="image/jpeg"))

    def test_a_boxed_still_is_served_as_a_jpeg(self, client):
        key = "synthetic/GA-0001/evidence.jpg"
        self._put(client, key, b"\xff\xd8stub-jpeg")

        response = client.get(f"/media/{key}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/jpeg")
        # Still labelled generated at the transport layer.
        assert response.headers["X-Content-Synthetic"] == "true"

    def test_a_non_generated_key_is_still_refused(self, client):
        """The prefix check is what keeps evidence and generated media apart."""
        assert client.get("/media/frames/GA/real.jpg").status_code == 404


class TestDashcam:
    """A phone camera, read by the same model, storing nothing.

    The storing-nothing part is the one that matters. Every other analysis path
    in this system exists to produce an auditable case; this one deliberately
    cannot, because a phone is not a registered camera and we do not know whose
    road it is on. If it ever starts writing, a report could be backed by a
    picture nobody kept.
    """

    JPEG = b"\xff\xd8\xff\xe0" + b"stub jpeg bytes"

    def _counts(self, client) -> dict:
        import sqlite3

        path = client.app.state.container.settings.sqlite_path
        conn = sqlite3.connect(path)
        try:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("cases", "detections", "frames", "filings", "cameras")
            }
        finally:
            conn.close()

    def test_the_page_renders(self, client):
        r = client.get("/dashcam")
        assert r.status_code == 200
        assert "dash-video" in r.text
        # The promise, stated on the page itself.
        assert "Nothing is stored" in r.text

    def test_it_looks_and_answers(self, client):
        r = client.post(
            "/api/dashcam/look", content=self.JPEG, headers={"Content-Type": "image/jpeg"}
        )
        assert r.status_code == 200
        body = r.json()
        assert "found" in body
        assert "model" in body

    def test_looking_writes_nothing(self, client):
        before = self._counts(client)
        for _ in range(3):
            client.post(
                "/api/dashcam/look", content=self.JPEG,
                headers={"Content-Type": "image/jpeg"},
            )
        assert self._counts(client) == before, "a dashcam look reached the database"

    def test_no_outbox_entry_is_ever_written(self, client):
        from pathlib import Path

        outbox = Path(client.app.state.container.settings.filing_outbox)
        before = set(outbox.rglob("*")) if outbox.is_dir() else set()
        client.post(
            "/api/dashcam/look", content=self.JPEG, headers={"Content-Type": "image/jpeg"}
        )
        after = set(outbox.rglob("*")) if outbox.is_dir() else set()
        assert after == before

    def test_an_empty_body_is_refused(self, client):
        r = client.post("/api/dashcam/look", content=b"")
        assert r.status_code == 422

    def test_something_that_is_not_a_jpeg_is_refused(self, client):
        """The analyzer forwards bytes with a hardcoded image/jpeg mime type, so
        a PNG would be a confusing failure much further down."""
        r = client.post("/api/dashcam/look", content=b"\x89PNG\r\n\x1a\n" + b"x" * 40)
        assert r.status_code == 415

    def test_an_oversized_frame_is_refused_with_a_number_to_act_on(self, client):
        from road_cleaner.web.app import DASHCAM_MAX_BYTES

        r = client.post(
            "/api/dashcam/look",
            content=b"\xff\xd8" + b"x" * (DASHCAM_MAX_BYTES + 1),
            headers={"Content-Type": "image/jpeg"},
        )
        assert r.status_code == 413
        assert "KB" in r.json()["detail"]

    def test_the_nav_offers_it(self, client):
        assert 'href="/dashcam"' in client.get("/").text


class TestSendHandsOverWithoutNavigating:
    """Send shows the request. It must not send anyone anywhere.

    The first version opened the agency's intake page in a tab. Every address in
    `seeds/agencies.yaml` is `example.invalid` by design, so that navigated the
    viewer off the demo and onto a DNS error page — and even against a real
    endpoint it would have replaced the filled-in request with a blank form.
    """

    def test_the_panel_is_on_the_page_and_starts_hidden(self, client, populated):
        _, cases = populated
        body = client.get(f"/cases/{cases[0].id}").text
        if "run__missing" in body:
            pytest.skip("this case has no clip")
        assert 'id="run-handover"' in body
        assert 'id="run-fields"' in body
        assert "hidden" in body.split('id="run-handover"')[1][:40]

    def test_the_page_never_ships_an_unconditional_redirect(self, client):
        """The mechanism, guarded at the source.

        `window.open` and a `location.href` assignment are how the old version
        left the page; neither belongs in the send path any more.
        """
        script = client.get("/static/js/inspect.js").text
        assert "window.open(" not in script
        assert "window.location.href =" not in script

    def test_a_non_routable_destination_is_not_offered_as_a_link(self, client):
        """`.invalid` is reserved by RFC 2606 so it can never resolve."""
        script = client.get("/static/js/inspect.js").text
        assert "\\.invalid" in script or ".invalid" in script

    def test_the_composed_fields_reach_the_page(self, client, populated):
        """The filled-in form is the artefact worth showing."""
        import html
        import json
        import re

        _, cases = populated
        for case in cases:
            body = client.get(f"/cases/{case.id}").text
            match = re.search(r"data-analysis='([^']*)'", body)
            if not match:
                continue
            data = json.loads(html.unescape(match.group(1)))
            assert "report_destination" in data
            assert "report_payload" in data
            return
        pytest.skip("no case in the fixture carries a cached analysis")


class TestTheDashcamCanReport:
    """The phone finds it; a person sends it.

    Nothing about this path may reach the filing machinery — the button hands the
    picture and the text to the device's own share sheet and stops there.
    """

    def test_the_report_button_ships_and_starts_hidden(self, client):
        body = client.get("/dashcam").text
        assert 'id="dash-report"' in body
        # A report button with nothing to report teaches people to ignore it.
        after = body.split('id="dash-report"')[1][:60]
        assert "hidden" in after

    def test_it_asks_for_a_location(self, client):
        script = client.get("/static/js/dashcam.js").text
        assert "navigator.geolocation" in script
        # And says so when it does not get one, rather than reporting a hazard
        # with no location a crew could act on.
        assert "No location" in script

    def test_it_shares_a_file_rather_than_pretending_mailto_can_attach(self, client):
        script = client.get("/static/js/dashcam.js").text
        assert "navigator.share" in script
        assert "canShare" in script
        # The desktop fallback is honest about what it cannot do.
        assert "cannot attach" in script

    def test_the_box_is_burned_into_the_shared_image(self, client):
        """A rectangle that only exists as a div is worth nothing in an inbox."""
        script = client.get("/static/js/dashcam.js").text
        assert "strokeRect" in script

    def test_the_page_no_longer_disowns_the_phone(self, client):
        """It used to say a phone is not a real camera and could not be filed —
        which directly contradicts having a Report button."""
        body = client.get("/dashcam").text
        assert "not a registered public camera" not in body
        assert "Nothing is stored" in body

    def test_hidden_actually_hides(self, client):
        """The UA rule loses to any author `display`, and `.dash__idle` set one."""
        css = client.get("/static/css/app.css").text
        assert "[hidden] { display: none !important; }" in css

    def test_the_viewfinder_does_not_crop_the_stream(self, client):
        """`object-fit: cover` threw away half a landscape frame and, worse,
        silently misaligned every box drawn over it."""
        css = client.get("/static/css/app.css").text
        dash = css[css.index(".dash__frame") : css.index(".dash__frame") + 400]
        assert "object-fit" not in dash
        assert "aspect-ratio" not in dash


class TestThePageSaysWhatTheSystemNowIs:
    """The product reads road footage, not a fleet of fixed cameras.

    The old framing was everywhere and some of it was load-bearing: the hero
    claimed two thousand cameras in three states, and the About page walked
    through a loop that only a fixed camera can perform.
    """

    def test_the_hero_does_not_claim_a_camera_fleet(self, client):
        body = client.get("/").text
        assert "Two thousand public traffic cameras" not in body
        assert "dashcam" in body.lower()

    def test_the_meta_description_matches(self, client):
        """It is what a link preview shows, so it outlives the page copy."""
        body = client.get("/").text
        assert "watches public traffic cameras across three states" not in body

    def test_about_no_longer_promises_three_states_on_duty(self, client):
        body = client.get("/about").text
        assert "Three states on duty" not in body

    def test_about_does_not_ask_the_model_for_a_lane(self, client):
        """We stopped asking, because from a road you cannot count lanes."""
        body = client.get("/about").text
        assert "which lane" not in body

    def test_about_does_not_promise_attachments_on_every_channel(self, client):
        """Two of the three channels post form fields and no files at all."""
        body = client.get("/about").text
        assert "timestamped frames attached" not in body

    def test_the_check_back_step_admits_a_dashcam_cannot_go_back(self, client):
        # Collapsed, because the sentence wraps in the template.
        body = " ".join(client.get("/about").text.split())
        assert "A dashcam pass has nothing to return to" in body
