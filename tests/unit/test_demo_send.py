"""The one path that actually transmits, and the fence around it.

`DemoSend` exists so the system can be shown finishing its last step instead of
described as able to. That makes it the only code here with a side effect
outside the process, so what it does and what it refuses to do both need
holding still.

The refusal half lives in `test_media.py::TestTheAllowlistIsNarrowerThanTheSwitch`
-- that an allowlisted address may send and `contact@dot.ga.gov` still may not.
This file covers the send itself: that the message is built the way a mail
server will accept, that the evidence really is attached, and that the gate's
verdict still decides whether anything leaves at all.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import patch

import pytest

import road_cleaner.config as cfg
from road_cleaner.pipeline.demo_send import DemoSend, DemoSendError
from road_cleaner.pipeline.drill import DrillResult, StageReport

RECIPIENT = "kylezemel@gmail.com"


class FakeSMTP:
    """Stands in for a mail server, and records what it was handed."""

    sent: dict = {}

    def __init__(self, host, port, timeout=None):
        FakeSMTP.sent = {"endpoint": (host, port)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        FakeSMTP.sent["starttls"] = True

    def login(self, user, password):
        FakeSMTP.sent["login"] = user

    def send_message(self, message):
        FakeSMTP.sent["message"] = message


@pytest.fixture
def media(tmp_path: Path) -> Path:
    folder = tmp_path / "synthetic" / "DEMO-1"
    folder.mkdir(parents=True)
    for name in ("a.jpg", "b.jpg"):
        (folder / name).write_bytes(b"\xff\xd8" + b"x" * 2048)
    return tmp_path


@pytest.fixture
def settings(media: Path, monkeypatch):
    made = cfg.Settings(
        ROAD_CLEANER_MODE="local",
        LIVE_FILING_ALLOWLIST=RECIPIENT,
        DEMO_SEND_TO=RECIPIENT,
        SMTP_HOST="smtp.gmail.com",
        SMTP_USER=RECIPIENT,
        SMTP_PASSWORD="an-app-password",
        FILING_FROM_ADDRESS=RECIPIENT,
        MEDIA_LOCAL_PATH=str(media),
    )
    # The guard reads settings through `get_settings`, not through the container.
    monkeypatch.setattr(cfg, "get_settings", lambda: made)
    return made


@pytest.fixture
def outcome() -> DrillResult:
    return DrillResult(
        stages=[StageReport("report", "Report", "done")],
        case_id="DEMO-1",
        spec={"state": "GA", "hazard_type": "pothole", "place": "I-75"},
        frame_urls=["/media/synthetic/DEMO-1/a.jpg", "/media/synthetic/DEMO-1/b.jpg"],
        agency="Georgia DOT — District 7",
        report_subject="Road hazard: pothole in a travel lane on I-75",
        report_body="Reporting a road hazard seen from a vehicle dashcam.\n\nA deep pothole.",
    )


async def _send(settings, outcome) -> tuple[str, int]:
    container = types.SimpleNamespace(settings=settings)
    with patch("smtplib.SMTP", FakeSMTP):
        return await DemoSend(container)._transmit(outcome, RECIPIENT)


class TestTheMessageThatLeaves:
    async def test_it_reaches_the_configured_mail_server(self, settings, outcome):
        await _send(settings, outcome)
        assert FakeSMTP.sent["endpoint"] == ("smtp.gmail.com", 587)
        assert FakeSMTP.sent["starttls"] is True, "Gmail refuses 587 without STARTTLS"
        assert FakeSMTP.sent["login"] == RECIPIENT

    async def test_it_is_addressed_to_the_allowlisted_recipient(self, settings, outcome):
        to, _ = await _send(settings, outcome)
        assert to == RECIPIENT
        assert FakeSMTP.sent["message"]["To"] == RECIPIENT

    async def test_the_evidence_is_genuinely_attached(self, settings, outcome):
        """The whole reason this path exists rather than a `mailto:` link.

        `mailto:` cannot carry a file under any circumstances, so an attached
        still is the one thing a real send can do that the rest of the site
        cannot. If this regresses, the demo proves less than the drill does.
        """
        _, counted = await _send(settings, outcome)
        parts = [
            p for p in FakeSMTP.sent["message"].walk()
            if p.get_content_maintype() == "image"
        ]
        assert [p.get_filename() for p in parts] == ["a.jpg", "b.jpg"]
        assert all(p.get_payload(decode=True).startswith(b"\xff\xd8") for p in parts)
        assert counted == 2

    async def test_a_frame_that_never_landed_is_not_counted(self, settings, outcome):
        """Counting keys rather than files would report a still that is not there."""
        outcome.frame_urls = [*outcome.frame_urls, "/media/synthetic/DEMO-1/gone.jpg"]
        _, counted = await _send(settings, outcome)
        assert counted == 2

    async def test_the_boxed_stills_are_preferred_over_the_raw_frames(
        self, settings, outcome, media: Path
    ):
        """A picture of a road is scenery until something marks the hazard.

        The case page draws its boxes in CSS, which does not survive the picture
        leaving the page -- so an emailed still has to carry the rectangle burned
        in or it asks a maintenance desk to spot a pothole unaided.
        """
        folder = media / "synthetic" / "DEMO-1"
        for name in ("boxed-a.jpg", "boxed-b.jpg"):
            (folder / name).write_bytes(b"\xff\xd8" + b"boxed" * 200)
        outcome.evidence_urls = [
            "/media/synthetic/DEMO-1/boxed-a.jpg",
            "/media/synthetic/DEMO-1/boxed-b.jpg",
        ]
        await _send(settings, outcome)
        names = [
            p.get_filename() for p in FakeSMTP.sent["message"].walk()
            if p.get_content_maintype() == "image"
        ]
        assert names == ["boxed-a.jpg", "boxed-b.jpg"]

    async def test_raw_frames_are_used_when_boxing_produced_nothing(
        self, settings, outcome
    ):
        """Boxing is non-fatal upstream, so its absence must not cost the stills."""
        outcome.evidence_urls = []
        _, counted = await _send(settings, outcome)
        assert counted == 2

    async def test_it_marks_itself_machine_generated(self, settings, outcome):
        message = (await _send(settings, outcome)) and FakeSMTP.sent["message"]
        assert message["Auto-Submitted"] == "auto-generated"


class TestTheBodyIsExactlyWhatWasComposed:
    """Nothing is appended to the report on its way out.

    An earlier version added a DEMONSTRATION footer naming the agency the rules
    had resolved to. It has been removed deliberately: the message is the report
    and the stills it was written about, and the send path is not a second place
    where report wording gets decided.
    """

    async def test_the_body_is_the_composed_report_verbatim(self, settings, outcome):
        await _send(settings, outcome)
        body = FakeSMTP.sent["message"].get_body(preferencelist=("plain",)).get_content()
        assert body.rstrip("\n") == outcome.report_body.rstrip("\n")

    async def test_nothing_is_appended_below_a_rule(self, settings, outcome):
        await _send(settings, outcome)
        body = FakeSMTP.sent["message"].get_body(preferencelist=("plain",)).get_content()
        assert "---" not in body
        assert "DEMONSTRATION" not in body


class TestTheGateStillDecides:
    """The first live run mailed a report the gate had refused.

    Two looks disagreed -- "pothole this time, debris before" -- so the gate
    returned `watch`, meaning not confident enough to report. The drill composes
    a draft anyway, which is right for a drill and wrong for the one path that
    transmits: a demonstration claiming to run the real gate while overriding it
    is demonstrating something the product does not do.
    """

    async def _run(self, settings, outcome, monkeypatch):
        from road_cleaner.pipeline import demo_send as module

        async def fake_run(self, text, *, full=False, pin=None, on_progress=None):
            if on_progress:
                await on_progress(outcome)
            return outcome

        monkeypatch.setattr(module.Drill, "run", fake_run)
        container = types.SimpleNamespace(settings=settings)
        with patch("smtplib.SMTP", FakeSMTP):
            FakeSMTP.sent = {}
            return await module.DemoSend(container).run("a pothole", to=RECIPIENT)

    @pytest.mark.parametrize("decision", ["watch", "suppress", "drop"])
    async def test_a_verdict_short_of_file_sends_nothing(
        self, settings, outcome, monkeypatch, decision
    ):
        outcome.gate_decision = decision
        outcome.gate_reason = "Looked twice and saw different things."
        result = await self._run(settings, outcome, monkeypatch)
        assert result.sent is False
        assert "message" not in FakeSMTP.sent, "a refused report reached the mail server"

    async def test_it_says_it_was_held_rather_than_that_it_failed(
        self, settings, outcome, monkeypatch
    ):
        """Held and failed are different outcomes; only one is a fault."""
        outcome.gate_decision = "watch"
        outcome.gate_reason = "Looked twice and saw different things."
        result = await self._run(settings, outcome, monkeypatch)
        assert result.gate_decision == "watch"
        assert "watch" in result.error
        assert result.stages[-1].state == "blocked"

    async def test_a_filed_verdict_does_send(self, settings, outcome, monkeypatch):
        outcome.gate_decision = "file"
        result = await self._run(settings, outcome, monkeypatch)
        assert result.sent is True
        assert FakeSMTP.sent["message"]["To"] == RECIPIENT


class TestItRefusesBeforeDoingAnyWork:
    """A misconfigured demo should fail in a second, not after a Veo render."""

    def _run(self, settings, to):
        container = types.SimpleNamespace(settings=settings)
        return DemoSend(container)._check_recipient(to)

    def test_an_address_not_on_the_allowlist_is_refused(self, settings):
        with pytest.raises(DemoSendError, match="LIVE_FILING_ALLOWLIST"):
            self._run(settings, "contact@dot.ga.gov")

    def test_an_empty_recipient_is_refused(self, settings):
        with pytest.raises(DemoSendError, match="No recipient"):
            self._run(settings, "")

    def test_a_missing_mail_server_is_refused(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "smtp_host", None)
        with pytest.raises(DemoSendError, match="SMTP_HOST"):
            self._run(settings, RECIPIENT)

    def test_the_configured_recipient_passes(self, settings):
        assert self._run(settings, RECIPIENT) == RECIPIENT
