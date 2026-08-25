"""Analysing one clip, frame by frame, and stopping before it sends.

Offline. The vision model is a fake, because what is worth testing is not that
Gemini works -- it is that the run wires the real gate, the real jurisdiction
lookup and the real report composition together, streams as it goes, tells the
truth about where its answers came from, and cannot file.

The clip is a real file decoded by real ffmpeg. Faking the decode would skip the
one part with moving pieces.
"""

from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

from road_cleaner.adapters.media.frame_extract import SampledFrame, ffmpeg_path
from road_cleaner.domain.enums import HazardType, Severity
from road_cleaner.domain.models import BoundingBox, Detection
from road_cleaner.pipeline.inspect import (
    CACHE_SUFFIX,
    CLIP_GATE,
    STAGES,
    STAGES_NO_SEND,
    InspectError,
    Inspector,
    _clearest,
    cached_analysis,
    clip_for_case,
)
from road_cleaner.ports.media import SYNTHETIC_PREFIX


def _detection(camera, frame, *, confidence=0.93, hazard=HazardType.DEBRIS, box=True):
    return Detection(
        camera_id=camera.id,
        frame_id=frame.id,
        analyzed_at=frame.captured_at,
        hazard_type=hazard,
        lane_position="lane_1",
        severity=Severity.MEDIUM,
        confidence=confidence,
        description="A shredded tyre tread is lying in the left travel lane.",
        box=BoundingBox(x=0.4, y=0.5, width=0.1, height=0.1) if box else None,
        box_is_measured=box,
        model_name="fake-vision",
    )


class FakeVision:
    """Confident about every frame it is handed."""

    def __init__(self, *, verdicts=None) -> None:
        self.calls = 0
        self.verdicts = verdicts

    async def prefilter(self, image, frame, camera):
        return True

    async def analyze(self, image, frame, camera):
        self.calls += 1
        if self.verdicts is not None:
            spec = self.verdicts[(self.calls - 1) % len(self.verdicts)]
            return None if spec is None else _detection(camera, frame, **spec)
        return _detection(camera, frame)


class ThrottledVision:
    """Vertex, out of quota."""

    async def prefilter(self, image, frame, camera):
        return True

    async def analyze(self, image, frame, camera):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")


@pytest.fixture
async def case_with_clip(container):
    """A case in the store with a real, decodable clip on disk.

    Built directly rather than by running the simulated pipeline. The pipeline
    route works but costs about ten seconds per test, and none of what it
    produces is under test here -- the Inspector only ever reads back a case,
    its camera, and a file.
    """
    from road_cleaner.domain.models import Camera, Case

    camera = Camera(
        id="NCDOT-CCTV-0112",
        state="NC",
        name="I-40 near Wade Ave",
        road="I-40",
        direction="westbound",
        lat=35.79,
        lng=-78.71,
        county="Wake",
        # Set so the `camera-owner` rule resolves without reaching for ADK.
        # Jurisdiction is exercised properly in `test_jurisdiction.py`.
        owner_agency_id="nc-dot-d5",
        snapshot_url="https://example.invalid/cam.jpg",
    )
    await container.repository.upsert_camera(camera)

    case = Case(
        id="NC-9001",
        camera_id=camera.id,
        state="NC",
        hazard_type=HazardType.DEBRIS,
        hazard_title="Debris in a travel lane",
        location="I-40 westbound near Wade Ave",
        severity=Severity.MEDIUM,
        confidence=0.9,
    )
    await container.repository.save_case(case)

    folder = Path(container.settings.media_local_path) / SYNTHETIC_PREFIX / case.id
    folder.mkdir(parents=True, exist_ok=True)
    clip = folder / "clip.mp4"
    subprocess.run(
        [
            ffmpeg_path(), "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=4:size=320x180:rate=24",
            "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True, capture_output=True,
    )
    return case, clip


# ------------------------------------------------------------------- shape


def test_there_is_no_send_stage_without_somewhere_to_send():
    """The default is still a run that ends at a composed report.

    That used to be unconditional, and this test asserted it as an invariant.
    It is now conditional: a deployment with an allowlisted demonstration inbox
    finishes the job, because a demonstration ending at a button asks the person
    watching to take the system's word for the only part they cannot verify.

    What must stay true is the direction of the default. Without a recipient
    there is no Send stage at all -- not a greyed-out one, which would read as
    having run out of time rather than having declined.
    """
    assert [k for k, _ in STAGES_NO_SEND] == [
        "sample", "look", "confirm", "resolve", "report",
    ]
    assert [k for k, _ in STAGES] == [*[k for k, _ in STAGES_NO_SEND], "send"]


class TestTheSendStageOnlyExistsWhereItCanBeUsed:
    """`_demo_recipient` is the single gate on whether a run may transmit.

    Both settings are required and they answer different questions -- one names
    a recipient, the other permits writing to it -- so neither alone may unlock
    a send.
    """

    def _inspector(self, **env):
        import road_cleaner.config as cfg
        from road_cleaner.pipeline.inspect import Inspector

        settings = cfg.Settings(ROAD_CLEANER_MODE="local", **env)
        return Inspector(types.SimpleNamespace(settings=settings))

    def test_nothing_configured_means_no_recipient(self):
        assert self._inspector()._demo_recipient() is None

    def test_a_recipient_without_the_allowlist_is_refused(self):
        got = self._inspector(
            DEMO_SEND_TO="kylezemel@gmail.com", SMTP_HOST="smtp.gmail.com",
            LIVE_FILING_ALLOWLIST="",
        )._demo_recipient()
        assert got is None

    def test_an_allowlist_without_a_recipient_sends_nowhere(self):
        got = self._inspector(
            LIVE_FILING_ALLOWLIST="kylezemel@gmail.com", SMTP_HOST="smtp.gmail.com",
            DEMO_SEND_TO="",
        )._demo_recipient()
        assert got is None

    def test_no_mail_server_means_no_recipient(self):
        got = self._inspector(
            DEMO_SEND_TO="kylezemel@gmail.com",
            LIVE_FILING_ALLOWLIST="kylezemel@gmail.com", SMTP_HOST="",
        )._demo_recipient()
        assert got is None

    def test_all_three_together_unlock_it(self):
        got = self._inspector(
            DEMO_SEND_TO="kylezemel@gmail.com",
            LIVE_FILING_ALLOWLIST="kylezemel@gmail.com",
            SMTP_HOST="smtp.gmail.com",
        )._demo_recipient()
        assert got == "kylezemel@gmail.com"

    def test_a_real_agency_address_can_never_become_the_recipient(self):
        """The allowlist is the fence, and it is checked against the recipient."""
        got = self._inspector(
            DEMO_SEND_TO="contact@dot.ga.gov",
            LIVE_FILING_ALLOWLIST="kylezemel@gmail.com",
            SMTP_HOST="smtp.gmail.com",
        )._demo_recipient()
        assert got is None


def test_the_clip_gate_relaxes_only_the_corroboration_window():
    """Every other threshold has to match the live gate exactly.

    The window is narrowed because five stills from one pass cannot be two
    camera polls 90 seconds apart. Quietly lowering the confidence floor at the
    same time would turn the demo into a machine for agreeing with itself.
    """
    from road_cleaner.domain.gating import GateConfig

    live = GateConfig()
    for name in ("min_confidence", "corroboration_min_confidence",
                 "duplicate_radius_meters", "watch_margin"):
        assert getattr(CLIP_GATE, name) == getattr(live, name), name
    assert CLIP_GATE.min_frame_gap_seconds == 0
    assert CLIP_GATE.max_frame_gap_seconds < live.min_frame_gap_seconds


# ------------------------------------------------------------- clip lookup


class TestFindingTheClip:
    def test_returns_none_when_the_case_has_no_media(self, tmp_path):
        assert clip_for_case(tmp_path, "GA-0001") is None
        assert clip_for_case(None, "GA-0001") is None

    def test_returns_none_when_the_folder_holds_no_video(self, tmp_path):
        folder = tmp_path / SYNTHETIC_PREFIX / "GA-0001"
        folder.mkdir(parents=True)
        (folder / "briefing.mp3").write_bytes(b"x")
        (folder / "clip.mp4.json").write_text("{}")
        assert clip_for_case(tmp_path, "GA-0001") is None

    def test_picks_the_newest_clip(self, tmp_path):
        import os
        import time

        folder = tmp_path / SYNTHETIC_PREFIX / "GA-0001"
        folder.mkdir(parents=True)
        old, new = folder / "old.mp4", folder / "new.mp4"
        for path in (old, new):
            path.write_bytes(b"x")
        os.utime(old, (time.time() - 600, time.time() - 600))
        assert clip_for_case(tmp_path, "GA-0001") == new


# ------------------------------------------------------------- the evidence


class TestChoosingTheEvidenceStill:
    """A dashcam approaches its hazard, so the first sighting is the smallest."""

    def _pair(self, index, confidence, size):
        detection = Detection(
            camera_id="c", frame_id="f", hazard_type=HazardType.DEBRIS,
            lane_position="lane_1", severity=Severity.MEDIUM, confidence=confidence,
            description="", box=BoundingBox(x=0.4, y=0.4, width=size, height=size),
            box_is_measured=True,
        )
        return SampledFrame(index=index, at_seconds=float(index), jpeg=b""), detection

    def test_equal_confidence_picks_the_closer_look(self):
        """The real NC-1169 shape: 0.95 on a 30px shape, 0.95 filling a tenth
        of the picture. The page has to show the one you can see."""
        chosen, _ = _clearest([self._pair(0, 0.95, 0.03), self._pair(2, 0.95, 0.11)])
        assert chosen.index == 2

    def test_a_real_confidence_gap_still_wins(self):
        chosen, _ = _clearest([self._pair(0, 0.95, 0.03), self._pair(2, 0.60, 0.30)])
        assert chosen.index == 0

    def test_nothing_measured_means_no_still(self):
        assert _clearest([]) is None


# ------------------------------------------------------------------ the run


class TestRefusals:
    async def test_an_unknown_case_says_so(self, container):
        with pytest.raises(InspectError, match="No case"):
            await Inspector(container).run("GA-9999")

    async def test_a_case_with_no_clip_says_what_to_do(self, container, case_with_clip):
        case, clip = case_with_clip
        clip.unlink()

        with pytest.raises(InspectError, match="no clip"):
            await Inspector(container).run(case.id)


class TestAFullRun:
    async def test_it_runs_every_stage_and_never_files(self, container, case_with_clip):
        case, _ = case_with_clip
        container.vision = FakeVision()

        result = await Inspector(container).run(case.id)

        assert [s.state for s in result.stages] == ["done"] * 5
        assert result.filed is False
        assert result.report_subject and result.report_body
        assert result.agency
        assert result.gate_decision

    async def test_no_report_is_written_to_the_outbox(self, container, case_with_clip):
        """`compose()` is the side-effect-free half. `transmit()` is never called."""
        case, _ = case_with_clip
        container.vision = FakeVision()
        outbox = Path(container.settings.filing_outbox)
        before = set(outbox.rglob("*")) if outbox.is_dir() else set()

        await Inspector(container).run(case.id)

        after = set(outbox.rglob("*")) if outbox.is_dir() else set()
        assert after == before, "the analysis put something in the outbox"

    async def test_one_vision_call_per_frame(self, container, case_with_clip):
        case, _ = case_with_clip
        vision = FakeVision()
        container.vision = vision

        result = await Inspector(container).run(case.id)

        assert vision.calls == len(result.frames)

    async def test_progress_is_published_as_frames_land(self, container, case_with_clip):
        """Not just per stage. Watching the boxes arrive is the whole feature."""
        case, _ = case_with_clip
        container.vision = FakeVision()

        counts = []

        async def on_progress(result):
            counts.append(sum(1 for f in result.frames if f.get("state") != "looking"))

        await Inspector(container).run(case.id, on_progress=on_progress)

        assert counts == sorted(counts), "frame count went backwards"
        assert len(set(counts)) > 2, "results appeared all at once instead of streaming"

    async def test_frames_carry_their_timestamp_and_box(self, container, case_with_clip):
        case, _ = case_with_clip
        container.vision = FakeVision()

        result = await Inspector(container).run(case.id)

        for row in result.frames:
            assert row["found"] is True
            assert row["box"]["width"] > 0
            assert row["box_measured"] is True
            assert 0 <= row["at"] <= 5
            assert row["stamp"].endswith("s")

    async def test_a_boxed_still_is_saved_for_the_page(self, container, case_with_clip):
        case, _ = case_with_clip
        container.vision = FakeVision()

        result = await Inspector(container).run(case.id)

        assert result.evidence_url and result.evidence_url.startswith("/media/")
        assert result.evidence_at is not None
        key = result.evidence_url.removeprefix("/media/")
        assert await container.media_blobs.get(key), "the still was not written"

    async def test_a_scripted_run_admits_it(self, container, case_with_clip):
        """Locally VISION_PROVIDER=auto is scripted while the deployment sets
        gemini, so the same code narrates two different things and the page has
        to be able to tell which one it is looking at."""
        case, _ = case_with_clip

        class Scripted(FakeVision):
            async def analyze(self, image, frame, camera):
                detection = await super().analyze(image, frame, camera)
                detection.model_name = "scripted"
                return detection

        container.vision = Scripted()
        result = await Inspector(container).run(case.id)

        assert result.is_scripted is True
        assert result.model_name == "scripted"

    async def test_the_flag_stays_true_until_a_model_says_otherwise(
        self, container, case_with_clip
    ):
        """The default has to be the cautious one.

        A run that finds nothing never reaches the line that sets provenance, so
        if `is_scripted` defaulted to False an empty run would claim a live
        model looked and cleared the road.
        """
        case, _ = case_with_clip
        container.vision = FakeVision(verdicts=[None])

        result = await Inspector(container).run(case.id)

        assert result.model_name is None
        assert result.is_scripted is True

    async def test_a_real_model_is_not_called_scripted(self, container, case_with_clip):
        case, _ = case_with_clip
        container.vision = FakeVision()
        result = await Inspector(container).run(case.id)
        assert result.is_scripted is False
        assert result.model_name == "fake-vision"


class TestWhenNothingIsFound:
    async def test_the_later_stages_are_blocked_not_failed(self, container, case_with_clip):
        """A clear road is an outcome, not an error."""
        case, _ = case_with_clip
        container.vision = FakeVision(verdicts=[None])

        result = await Inspector(container).run(case.id)

        states = {s.key: s.state for s in result.stages}
        assert states["sample"] == "done"
        assert states["look"] == "done"
        assert states["confirm"] == "blocked"
        assert states["report"] == "blocked"
        assert result.report_body is None
        assert result.filed is False


class TestTheCache:
    async def test_a_run_is_cached_beside_its_clip(self, container, case_with_clip):
        case, clip = case_with_clip
        container.vision = FakeVision()

        await Inspector(container).run(case.id)

        sidecar = clip.with_name(clip.name + CACHE_SUFFIX)
        assert sidecar.is_file()
        assert json.loads(sidecar.read_text())["case_id"] == case.id

    async def test_a_throttled_run_replays_the_last_one_and_says_so(
        self, container, case_with_clip
    ):
        """Vertex throttles at twenty-odd calls and a judge may click twice.

        A labelled replay beats an error page. A *silent* replay would make this
        feature the mockup it exists to replace, so `from_cache` is asserted.
        """
        case, _ = case_with_clip
        container.vision = FakeVision()
        live = await Inspector(container).run(case.id)

        container.vision = ThrottledVision()
        replayed = await Inspector(container).run(case.id)

        assert replayed.from_cache is True
        assert "unavailable" in (replayed.cache_note or "")
        assert replayed.report_body == live.report_body
        assert len(replayed.frames) == len(live.frames)
        assert [s.state for s in replayed.stages] == [s.state for s in live.stages]

    async def test_a_throttled_run_with_no_cache_fails_loudly(
        self, container, case_with_clip
    ):
        case, clip = case_with_clip
        assert cached_analysis(clip) is None
        container.vision = ThrottledVision()

        with pytest.raises(InspectError, match="Analysis failed"):
            await Inspector(container).run(case.id)

    async def test_a_corrupt_cache_is_ignored_rather_than_crashing(
        self, container, case_with_clip
    ):
        case, clip = case_with_clip
        clip.with_name(clip.name + CACHE_SUFFIX).write_text("{not json")
        assert cached_analysis(clip) is None

        container.vision = ThrottledVision()
        with pytest.raises(InspectError):
            await Inspector(container).run(case.id)


class TestWhichFrameTheGateRules_On:
    """A clip is one pass that already happened, not a live camera.

    `evaluate` normally takes the newest detection because on a fixed camera the
    newest look is the one you act on. The last sample of a clip is often taken
    after the car has driven past the hazard, so judging the pass by it threw
    away the pass. FL-2196 called a coned-off lane a closure in three frames at
    0.88/0.92/0.94 and a lone cone `debris` in the fourth -- and came back
    "looked twice and saw different things".
    """

    def _found(self, *specs):
        from road_cleaner.adapters.media.frame_extract import SampledFrame  # noqa: F401

        out = []
        for index, (hazard, confidence) in enumerate(specs):
            out.append(
                Detection(
                    camera_id="c", frame_id=f"f{index}", hazard_type=hazard,
                    lane_position="unknown", severity=Severity.MEDIUM,
                    confidence=confidence, description="",
                )
            )
        return out

    def test_the_majority_decides_not_the_last_frame(self):
        from road_cleaner.pipeline.inspect import _subject_and_priors

        found = self._found(
            (HazardType.UNREPORTED_CLOSURE, 0.88),
            (HazardType.UNREPORTED_CLOSURE, 0.92),
            (HazardType.UNREPORTED_CLOSURE, 0.94),
            (HazardType.DEBRIS, 0.96),   # the car has passed; a lone cone
        )
        subject, priors = _subject_and_priors(found)

        assert subject.hazard_type is HazardType.UNREPORTED_CLOSURE
        assert subject.confidence == pytest.approx(0.94), "not the most confident of the majority"
        assert len(priors) == 2
        assert all(p.hazard_type is HazardType.UNREPORTED_CLOSURE for p in priors)

    def test_the_odd_frame_out_does_not_corroborate(self):
        """Dropping it is stricter than counting it, not looser."""
        from road_cleaner.pipeline.inspect import _subject_and_priors

        found = self._found(
            (HazardType.DEBRIS, 0.90),
            (HazardType.DEBRIS, 0.80),
            (HazardType.FLOODING, 0.99),
        )
        subject, priors = _subject_and_priors(found)

        assert subject.hazard_type is HazardType.DEBRIS
        assert len(priors) == 1
        assert all(p.hazard_type is HazardType.DEBRIS for p in priors)

    def test_a_single_frame_has_nothing_to_corroborate_it(self):
        from road_cleaner.pipeline.inspect import _subject_and_priors

        subject, priors = _subject_and_priors(self._found((HazardType.DEBRIS, 0.9)))
        assert priors == []
        assert subject.confidence == pytest.approx(0.9)

    async def test_a_mostly_agreeing_pass_now_clears_the_gate(self, container, case_with_clip):
        """End to end: the FL-2196 shape reaches `file`, not `watch`."""
        case, _ = case_with_clip
        container.vision = FakeVision(verdicts=[
            {"hazard": HazardType.DEBRIS, "confidence": 0.93},
            {"hazard": HazardType.DEBRIS, "confidence": 0.91},
            {"hazard": HazardType.DEBRIS, "confidence": 0.95},
            {"hazard": HazardType.DEBRIS, "confidence": 0.90},
            {"hazard": HazardType.FLOODING, "confidence": 0.99},
        ])

        result = await Inspector(container).run(case.id)

        assert result.gate_decision == "file"
        assert "frames agree" in (result.gate_reason or "")


class TestWhereItWouldGo:
    """The Send button needs a real target, not a description of one.

    `compose()` already works this out per channel and the answer used to be
    dropped on the next line. These pin the three shapes so a fourth copy of the
    rules never gets written somewhere else.
    """

    def _agency(self, channel, **kw):
        from road_cleaner.domain.enums import AgencyLevel, Channel
        from road_cleaner.domain.models import Agency

        return Agency(
            id="x", name="Test Agency", level=AgencyLevel.STATE_DOT, state="NC",
            channel=Channel(channel), **kw,
        )

    def _case(self):
        from road_cleaner.domain.models import Case

        return Case(
            id="NC-0001", camera_id="c", state="NC", hazard_type=HazardType.DEBRIS,
            hazard_title="Debris in a travel lane", location="I-40",
        )

    def test_an_email_agency_gives_an_address(self, container):
        from road_cleaner.pipeline.inspect import destination_for

        agency = self._agency("email", email="district5@example.invalid")
        destination, channel, _ = destination_for(self._case(), agency, container.settings)
        assert destination == "district5@example.invalid"
        assert channel == "email"

    def test_a_form_agency_gives_the_intake_url(self, container):
        from road_cleaner.pipeline.inspect import destination_for

        agency = self._agency("maintenance_form", endpoint="https://example.invalid/ncdot/div5")
        destination, channel, payload = destination_for(
            self._case(), agency, container.settings
        )
        assert destination == "https://example.invalid/ncdot/div5"
        assert channel == "maintenance_form"
        # The filled-in fields, which are what the page shows instead of
        # navigating anyone to a blank form.
        assert payload["issueCategory"] == "debris"
        assert payload["requestType"] == "roadway_maintenance"

    def test_an_open311_agency_gets_the_requests_path(self, container):
        """Open311 posts to `{endpoint}/requests.json`, not to the endpoint."""
        from road_cleaner.pipeline.inspect import destination_for

        agency = self._agency("open311", endpoint="https://example.invalid/durham/open311/v2")
        destination, _, payload = destination_for(self._case(), agency, container.settings)
        assert destination.endswith("/requests.json")
        assert payload["service_code"]

    def test_an_agency_with_no_contact_details_yields_nothing_not_a_crash(self, container):
        from road_cleaner.pipeline.inspect import destination_for

        destination, channel, _ = destination_for(
            self._case(), self._agency("email"), container.settings
        )
        assert destination == ""
        assert channel == "email"

    async def test_a_run_carries_the_destination_to_the_page(self, container, case_with_clip):
        case, _ = case_with_clip
        container.vision = FakeVision()

        result = await Inspector(container).run(case.id)

        assert result.report_channel == "maintenance_form"
        assert result.report_destination.startswith("https://")
        # And it survives into the payload the browser actually reads.
        assert result.as_dict()["report_destination"] == result.report_destination


class TestTheAutomaticSend:
    """The last step, taken without a button.

    A demonstration that ends at a control asks the person watching to take the
    system's word for the only part they cannot verify, so a run that clears the
    gate finishes the job. What must not change is what decides: the gate, and
    the allowlist.
    """

    class FakeSMTP:
        sent: dict = {}

        def __init__(self, host, port, timeout=None):
            TestTheAutomaticSend.FakeSMTP.sent = {"endpoint": (host, port)}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            TestTheAutomaticSend.FakeSMTP.sent["starttls"] = True

        def login(self, user, password):
            TestTheAutomaticSend.FakeSMTP.sent["login"] = user

        def send_message(self, message):
            TestTheAutomaticSend.FakeSMTP.sent["message"] = message

    async def _run(self, tmp_path, monkeypatch, *, decision="file", evidence=True):
        from unittest.mock import patch

        import road_cleaner.config as cfg
        from road_cleaner.domain.enums import AgencyLevel, Channel
        from road_cleaner.domain.models import Agency, Case
        from road_cleaner.pipeline.inspect import STAGES, InspectResult, Inspector

        folder = tmp_path / SYNTHETIC_PREFIX / "GA-1"
        folder.mkdir(parents=True)
        (folder / "evidence.jpg").write_bytes(b"\xff\xd8" + b"x" * 900)

        settings = cfg.Settings(
            ROAD_CLEANER_MODE="local", MEDIA_LOCAL_PATH=str(tmp_path),
            DEMO_SEND_TO="kylezemel@gmail.com",
            LIVE_FILING_ALLOWLIST="kylezemel@gmail.com",
            SMTP_HOST="smtp.gmail.com", SMTP_USER="bot@example.test",
            SMTP_PASSWORD="pw", FILING_FROM_ADDRESS="bot@example.test",
        )
        monkeypatch.setattr(cfg, "get_settings", lambda: settings)
        inspector = Inspector(types.SimpleNamespace(settings=settings))

        result = InspectResult(
            stages=[], case_id="GA-1",
            gate_decision=decision,
            report_subject="Road hazard: pothole in a travel lane on I-75",
            report_body="Reporting a road hazard seen from a vehicle dashcam.",
            evidence_url="/media/synthetic/GA-1/evidence.jpg" if evidence else None,
        )
        by_key = {k: __import__(
            "road_cleaner.pipeline.drill", fromlist=["StageReport"]
        ).StageReport(k, label) for k, label in STAGES}
        case = Case(
            id="GA-1", camera_id="c", state="GA", hazard_type=HazardType.DEBRIS,
            hazard_title="t", location="I-75",
        )
        agency = Agency(
            id="ga-dot-d7", name="Georgia DOT — District 7", level=AgencyLevel.STATE_DOT,
            state="GA", channel=Channel.MAINTENANCE_FORM, email="contact@dot.ga.gov",
        )

        async def publish():
            return None

        TestTheAutomaticSend.FakeSMTP.sent = {}
        with patch("smtplib.SMTP", TestTheAutomaticSend.FakeSMTP):
            await inspector._send(
                result, by_key, case, agency, "kylezemel@gmail.com", publish
            )
        return result, by_key

    async def test_a_cleared_run_sends_without_anything_being_pressed(
        self, tmp_path, monkeypatch
    ):
        result, by_key = await self._run(tmp_path, monkeypatch)
        assert result.filed is True
        assert result.sent_to == "kylezemel@gmail.com"
        assert by_key["send"].state == "done"
        assert "Delivered to kylezemel@gmail.com" in by_key["send"].detail

    async def test_it_goes_to_the_allowlisted_inbox_not_the_agency(
        self, tmp_path, monkeypatch
    ):
        """The agency is resolved for real and is still not the recipient."""
        await self._run(tmp_path, monkeypatch)
        message = TestTheAutomaticSend.FakeSMTP.sent["message"]
        assert message["To"] == "kylezemel@gmail.com"
        assert "dot.ga.gov" not in message["To"]

    async def test_the_boxed_still_rides_along(self, tmp_path, monkeypatch):
        await self._run(tmp_path, monkeypatch)
        parts = [
            p for p in TestTheAutomaticSend.FakeSMTP.sent["message"].walk()
            if p.get_content_maintype() == "image"
        ]
        assert len(parts) == 1
        assert parts[0].get_payload(decode=True).startswith(b"\xff\xd8")

    @pytest.mark.parametrize("decision", ["watch", "suppress", "drop"])
    async def test_a_verdict_short_of_file_sends_nothing(
        self, tmp_path, monkeypatch, decision
    ):
        """Automatic must not mean unconditional. The gate still decides."""
        result, by_key = await self._run(tmp_path, monkeypatch, decision=decision)
        assert result.filed is False
        assert by_key["send"].state == "blocked"
        assert "message" not in TestTheAutomaticSend.FakeSMTP.sent

    async def test_a_missing_still_does_not_stop_the_report(
        self, tmp_path, monkeypatch
    ):
        """A report with no picture beats no report."""
        result, by_key = await self._run(tmp_path, monkeypatch, evidence=False)
        assert result.filed is True
        assert "no still attached" in by_key["send"].detail
