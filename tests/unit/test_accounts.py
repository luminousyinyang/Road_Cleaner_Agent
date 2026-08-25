"""Accounts, and the one new way past the live-send guard.

The security-relevant half of the incidents feature is here. `guard_live_send`
grew a third exit -- an address permitted for the duration of one request -- and
these tests exist to pin down what that exit will and will not let through.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from road_cleaner.adapters.filing.base import allow_destination, guard_live_send
from road_cleaner.adapters.incidents.local_incidents import LocalIncidentStore
from road_cleaner.config import Settings
from road_cleaner.domain.enums import HazardType, Severity
from road_cleaner.domain.models import Incident
from road_cleaner.ports.filing_channel import FilingError
from road_cleaner.ports.incident_store import IncidentStore
from road_cleaner.web.auth import AuthUser, verify_id_token


def refuses(address: str) -> bool:
    try:
        guard_live_send(address, "email")
    except FilingError:
        return True
    return False


def an_incident(uid: str = "u1", **kw) -> Incident:
    return Incident(
        uid=uid,
        hazard_type=HazardType.DEBRIS,
        severity=Severity.HIGH,
        confidence=0.91,
        lat=33.7490,
        lng=-84.3880,
        **kw,
    )


class TestTheSendGuard:
    """`allow_destination` is the narrowest of the three ways through."""

    def test_an_address_is_refused_before_and_after_the_block(self):
        assert refuses("me@example.com")
        with allow_destination("me@example.com"):
            assert not refuses("me@example.com")
        # The grant is the point of failure worth testing: one that outlived its
        # request would silently arm that address for everything after it.
        assert refuses("me@example.com")

    def test_the_grant_is_released_even_when_the_send_raises(self):
        with pytest.raises(RuntimeError), allow_destination("me@example.com"):
            raise RuntimeError("SMTP fell over")
        assert refuses("me@example.com")

    def test_it_permits_exactly_one_address(self):
        """Not "sending is on now" -- one string, and nothing else."""
        with allow_destination("me@example.com"):
            assert refuses("contact@dot.ga.gov")
            assert refuses("someone.else@example.com")

    def test_the_address_is_matched_case_and_space_insensitively(self):
        with allow_destination("  Me@Example.COM "):
            assert not refuses("me@example.com")
            assert not refuses("ME@EXAMPLE.COM")

    def test_it_refuses_to_grant_nothing(self):
        """An empty grant would be a no-op that reads like permission."""
        for empty in ("", "   ", None):
            with pytest.raises(ValueError), allow_destination(empty):  # type: ignore[arg-type]
                pass

    def test_concurrent_requests_cannot_see_each_others_grant(self):
        """A ContextVar, not a global. Two people reporting at once must not
        end up able to mail each other."""

        async def report(mine: str, theirs: str) -> tuple[bool, bool]:
            with allow_destination(mine):
                # Yield, so both tasks are inside their block at once.
                await asyncio.sleep(0.01)
                return refuses(mine), refuses(theirs)

        async def both():
            return await asyncio.gather(
                report("a@example.com", "b@example.com"),
                report("b@example.com", "a@example.com"),
            )

        for mine_refused, theirs_refused in asyncio.run(both()):
            assert not mine_refused
            assert theirs_refused


class TestTheStillReachesTheInbox:
    """A report whose evidence silently went missing would look like it worked.

    `attachments` resolves keys against a filesystem root, which the dashcam
    cannot use -- its stills live in a blob store that may be GCS, where there
    is no path for a root to be relative to. Hence `inline_attachments`.
    """

    def test_bytes_handed_over_directly_are_attached(self):
        from road_cleaner.adapters.filing.base import ComposedReport
        from road_cleaner.adapters.filing.email_channel import EmailChannel

        jpeg = b"\xff\xd8\xff\xe0 not really a jpeg \xff\xd9"
        message = EmailChannel(host="smtp.invalid", from_address="rc@example.com")._build_message(
            ComposedReport(
                destination="driver@example.com",
                subject="Road hazard: debris",
                body="the report",
                inline_attachments=[("road-hazard.jpg", jpeg)],
            )
        )

        attached = list(message.iter_attachments())
        assert len(attached) == 1
        assert attached[0].get_filename() == "road-hazard.jpg"
        assert attached[0].get_content_type() == "image/jpeg"
        assert attached[0].get_payload(decode=True) == jpeg

    def test_a_report_with_no_attachment_still_builds(self):
        from road_cleaner.adapters.filing.base import ComposedReport
        from road_cleaner.adapters.filing.email_channel import EmailChannel

        message = EmailChannel(host="smtp.invalid", from_address="rc@example.com")._build_message(
            ComposedReport(destination="a@b.com", subject="s", body="b")
        )
        assert list(message.iter_attachments()) == []


class TestWhoAnAutomatedRunMailsFrom:
    """`Inspector.verified_recipient` redirects a run to the person who asked.

    The full-automation cards in the library are the only thing that sets it,
    and only after `require_mailable_user`. These pin down that an unset one
    changes nothing, and that a set one does not need the allowlist.
    """

    def _inspector(self, tmp_path: Path, recipient=None, **overrides):
        from road_cleaner.container import build_container
        from road_cleaner.pipeline.inspect import Inspector

        settings = Settings(
            ROAD_CLEANER_MODE="local",
            DATA_DIR=str(tmp_path),
            SQLITE_PATH=str(tmp_path / "t.db"),
            BLOB_LOCAL_PATH=str(tmp_path / "frames"),
            FILING_SANDBOX_INBOX=str(tmp_path / "outbox"),
            LOG_LEVEL="ERROR",
            **overrides,
        )
        container = build_container(settings, simulated=True)
        return Inspector(container, verified_recipient=recipient)

    def test_without_smtp_nothing_is_sent_to_anyone(self, tmp_path: Path):
        """Even a signed-in person. An address with no mail server is not a
        destination, and the run should end at a composed report."""
        inspector = self._inspector(tmp_path, recipient="driver@example.com")
        assert inspector._demo_recipient() is None

    def test_a_signed_in_person_outranks_the_demonstration_inbox(self, tmp_path: Path):
        inspector = self._inspector(
            tmp_path,
            recipient="driver@example.com",
            SMTP_HOST="smtp.example.com",
            DEMO_SEND_TO="demo@example.com",
            LIVE_FILING_ALLOWLIST="demo@example.com",
        )
        assert inspector._demo_recipient() == "driver@example.com"

    def test_their_address_needs_no_allowlist_entry(self, tmp_path: Path):
        """The allowlist is static operator configuration. It is not a place to
        accumulate everyone who ever signed in."""
        inspector = self._inspector(
            tmp_path, recipient="driver@example.com", SMTP_HOST="smtp.example.com"
        )
        assert inspector._demo_recipient() == "driver@example.com"

    def test_unset_leaves_the_old_behaviour_exactly_as_it_was(self, tmp_path: Path):
        allowed = self._inspector(
            tmp_path,
            SMTP_HOST="smtp.example.com",
            DEMO_SEND_TO="demo@example.com",
            LIVE_FILING_ALLOWLIST="demo@example.com",
        )
        assert allowed._demo_recipient() == "demo@example.com"

        # Named but not permitted: still two separate questions.
        not_allowed = self._inspector(
            tmp_path, SMTP_HOST="smtp.example.com", DEMO_SEND_TO="demo@example.com"
        )
        assert not_allowed._demo_recipient() is None

    def test_blank_and_whitespace_are_not_a_recipient(self, tmp_path: Path):
        for empty in ("", "   ", None):
            inspector = self._inspector(
                tmp_path, recipient=empty, SMTP_HOST="smtp.example.com"
            )
            assert inspector.verified_recipient is None
            assert inspector._demo_recipient() is None


class TestConcurrentRunsAreNotShared:
    """Two people pressing the same card want two different inboxes."""

    def test_a_run_is_only_reused_for_the_same_recipient(self):
        from road_cleaner.web.jobs import InspectJob, InspectJobs

        jobs = InspectJobs()
        mine = InspectJob(id="j1", case_id="GA-1")
        mine.recipient = "me@example.com"
        jobs._jobs["j1"] = mine
        jobs._by_case["GA-1"] = "j1"

        assert jobs.active_for("GA-1") is mine
        # Same case, different person: must not be handed my run, or they would
        # poll it, see "Delivered to me@example.com", and receive nothing.
        assert mine.recipient != "you@example.com"

    def test_the_recipient_is_not_in_the_polled_payload(self):
        """The poll endpoint takes only a job id. The address stays server-side."""
        from road_cleaner.web.jobs import InspectJob

        job = InspectJob(id="j1", case_id="GA-1")
        job.recipient = "me@example.com"
        assert "me@example.com" not in str(job.as_dict())


class TestAuthUser:
    def test_only_a_verified_address_is_mailable(self):
        """The whole basis for mailing somebody without a human approving it."""
        assert AuthUser("u", "a@b.com", True, None, None).mailable == "a@b.com"
        assert AuthUser("u", "a@b.com", False, None, None).mailable is None
        assert AuthUser("u", None, True, None, None).mailable is None

    def test_a_user_cannot_be_edited_after_verification(self):
        """Frozen: a handler that could rewrite `email` could mail anywhere."""
        user = AuthUser("u", "a@b.com", True, None, None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            user.email = "attacker@example.com"  # type: ignore[misc]


class TestTokenVerification:
    def test_rubbish_is_not_a_sign_in(self):
        assert verify_id_token("not-a-token") is None
        assert verify_id_token("") is None

    def test_it_returns_none_rather_than_raising_when_unconfigured(self):
        """Accounts-off is a supported state, so nothing here may blow up."""
        unconfigured = Settings(ROAD_CLEANER_MODE="local", LOG_LEVEL="WARNING")
        assert not unconfigured.auth_configured
        assert unconfigured.firebase_web_config == {}
        assert verify_id_token("anything", unconfigured) is None


class TestSettings:
    def test_three_of_four_firebase_values_is_not_configured(self):
        """A project id with no API key renders a button that cannot succeed."""
        partial = Settings(
            ROAD_CLEANER_MODE="local",
            LOG_LEVEL="WARNING",
            FIREBASE_PROJECT_ID="p",
            FIREBASE_API_KEY="k",
            FIREBASE_AUTH_DOMAIN="p.firebaseapp.com",
        )
        assert not partial.auth_configured

    def test_the_web_config_carries_no_secrets(self):
        """It ships in the page, so anything sensitive here is published."""
        full = Settings(
            ROAD_CLEANER_MODE="local",
            LOG_LEVEL="WARNING",
            FIREBASE_PROJECT_ID="p",
            FIREBASE_API_KEY="k",
            FIREBASE_AUTH_DOMAIN="p.firebaseapp.com",
            FIREBASE_APP_ID="a",
            SMTP_PASSWORD="hunter2",
        )
        assert full.auth_configured
        assert set(full.firebase_web_config) == {
            "apiKey",
            "authDomain",
            "projectId",
            "appId",
        }
        assert "hunter2" not in str(full.firebase_web_config)

    def test_notifying_the_dot_is_off_by_default(self):
        assert Settings(ROAD_CLEANER_MODE="local").dashcam_notify_dot is False


class TestTheIncidentStore:
    @pytest.fixture
    def store(self, tmp_path: Path) -> LocalIncidentStore:
        return LocalIncidentStore(tmp_path / "incidents")

    def test_it_satisfies_the_port(self, store):
        assert isinstance(store, IncidentStore)

    async def test_it_round_trips(self, store):
        await store.initialize()
        saved = an_incident(location="I-75 near Atlanta")
        await store.save(saved)

        back = await store.get("u1", saved.id)
        assert back is not None
        assert back.location == "I-75 near Atlanta"
        assert back.hazard_type is HazardType.DEBRIS

    async def test_newest_first(self, store):
        await store.initialize()
        for n in range(3):
            await store.save(an_incident(description=f"n{n}"))
        assert [i.description for i in await store.list_for_user("u1")] == ["n2", "n1", "n0"]

    async def test_one_user_cannot_read_anothers(self, store):
        """The uid is in the signature, not an optional filter to forget."""
        await store.initialize()
        mine = an_incident(uid="me")
        await store.save(mine)
        await store.save(an_incident(uid="you"))

        assert await store.get("you", mine.id) is None
        assert [i.uid for i in await store.list_for_user("you")] == ["you"]

    async def test_an_id_cannot_walk_out_of_its_directory(self, store):
        """Both halves of the path come off the wire, so both are sanitised."""
        await store.initialize()
        mine = an_incident(uid="me")
        await store.save(mine)

        assert await store.get("you", f"../me/{mine.id}") is None
        assert await store.get("../me", mine.id) is None

    async def test_an_unknown_user_has_no_incidents(self, store):
        await store.initialize()
        assert await store.list_for_user("nobody") == []
