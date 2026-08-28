"""Saving a dashcam finding, end to end.

Sign-in is stubbed with FastAPI's `dependency_overrides` rather than a real
Google token -- these tests are about what happens *after* somebody is verified.
The verification itself is covered in `tests/unit/test_accounts.py`, and the
refusals for unauthenticated callers in `test_web.py`.

SMTP is deliberately left unconfigured. That exercises the more interesting
half: an incident whose mail could not go out must still be saved, because
losing somebody's report because a mail server was down is the worse of the two
failures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from road_cleaner.config import Settings
from road_cleaner.web.app import create_app
from road_cleaner.web.auth import AuthUser, require_mailable_user, require_user

# A one-pixel JPEG. Enough to be stored and attached; the vision model never
# sees it, because the finding is described in `meta` rather than re-analysed.
JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
    "c00011080001000103012200021101031101ffc4001f0000010501"
    "010101010100000000000000000102030405060708090a0bffda00"
    "0c03010002110311003f00bf8001ffd9"
)

SOMEBODY = AuthUser(
    uid="uid-abc123",
    email="driver@example.com",
    email_verified=True,
    name="A Driver",
    picture=None,
)

# Downtown Atlanta -- inside the seeded place data, so it resolves to a real
# agency rather than 422ing out of coverage.
ATLANTA = {"lat": 33.7490, "lng": -84.3880}


def meta(**overrides) -> str:
    body = {
        **ATLANTA,
        "hazard": "debris",
        "severity": "high",
        "confidence": 0.91,
        "description": "a shed truck tyre in the right lane",
        "model": "test",
        "box": {"x": 0.4, "y": 0.55, "width": 0.15, "height": 0.12},
        "box_measured": True,
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ROAD_CLEANER_MODE="local",
        DRY_RUN=True,
        DATA_DIR=str(tmp_path),
        SQLITE_PATH=str(tmp_path / "test.db"),
        BLOB_LOCAL_PATH=str(tmp_path / "frames"),
        FILING_SANDBOX_INBOX=str(tmp_path / "outbox"),
        LOG_LEVEL="ERROR",
        FIREBASE_PROJECT_ID="test-project",
        FIREBASE_API_KEY="test-key",
        FIREBASE_AUTH_DOMAIN="test-project.firebaseapp.com",
        FIREBASE_APP_ID="1:2:web:3",
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    app.dependency_overrides[require_user] = lambda: SOMEBODY
    app.dependency_overrides[require_mailable_user] = lambda: SOMEBODY
    with TestClient(app) as c:
        yield c


def save(client, **overrides):
    return client.post(
        "/api/incidents",
        data={"meta": meta(**overrides)},
        files={"image": ("road-hazard.jpg", JPEG, "image/jpeg")},
    )


class TestSavingOne:
    def test_it_saves_and_comes_back(self, client):
        r = save(client)
        assert r.status_code == 201, r.text

        body = r.json()
        assert body["hazard"] == "Debris"
        assert body["confidence"] == 0.91
        assert body["description"] == "a shed truck tyre in the right lane"
        # Resolved through the same jurisdiction registry a real case uses.
        assert body["agency"]
        assert body["location"]

    def test_the_still_is_stored_and_served_back(self, client):
        incident = save(client).json()
        assert incident["image_url"] == f"/api/incidents/{incident['id']}/image"

        r = client.get(incident["image_url"])
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == JPEG
        # Somebody's own photograph: a shared cache must not hand it to whoever
        # asks for the same URL next.
        assert "private" in r.headers.get("cache-control", "")

    def test_the_box_survives_the_round_trip(self, client):
        """It is what makes the picture worth keeping."""
        incident = save(client).json()
        assert incident["box"] == {"x": 0.4, "y": 0.55, "width": 0.15, "height": 0.12}
        assert incident["box_measured"] is True

    def test_it_appears_in_the_list(self, client):
        saved = save(client).json()
        listed = client.get("/api/incidents").json()["incidents"]
        assert [i["id"] for i in listed] == [saved["id"]]

    def test_newest_first(self, client):
        for n in range(3):
            save(client, description=f"n{n}")
        listed = client.get("/api/incidents").json()["incidents"]
        assert [i["description"] for i in listed] == ["n2", "n1", "n0"]

    def test_a_report_is_composed_in_the_systems_own_words(self, client):
        """Not a second, parallel way of writing one."""
        incident = save(client).json()
        assert incident["subject"]
        assert "tyre" in incident["body"] or "debris" in incident["body"].lower()


class TestWhenTheMailCannotGo:
    """SMTP is unset in these tests, so nothing can actually be sent."""

    def test_the_incident_is_still_saved(self, client):
        r = save(client)
        assert r.status_code == 201
        assert client.get("/api/incidents").json()["incidents"]

    def test_and_it_does_not_claim_to_have_sent_it(self, client):
        assert save(client).json()["emailed_to"] is None


class TestNotifyingTheDot:
    def test_it_is_held_by_default(self, client):
        incident = save(client).json()
        assert incident["dot_state"] == "held"
        assert incident["dot_destination"] is None

    def test_turning_the_flag_on_does_not_open_the_wall(self, tmp_path: Path):
        """The safety claim, stated as a test.

        DASHCAM_NOTIFY_DOT opens the code path. The agency address still has to
        clear `guard_live_send` on its own -- through LIVE_FILING_ALLOWLIST or
        ALLOW_LIVE_FILING -- so flipping this flag by itself can never put mail
        in a real maintenance desk's inbox.
        """
        settings = Settings(
            ROAD_CLEANER_MODE="local",
            DRY_RUN=True,
            DATA_DIR=str(tmp_path),
            SQLITE_PATH=str(tmp_path / "t.db"),
            BLOB_LOCAL_PATH=str(tmp_path / "frames"),
            FILING_SANDBOX_INBOX=str(tmp_path / "outbox"),
            LOG_LEVEL="ERROR",
            FIREBASE_PROJECT_ID="p",
            FIREBASE_API_KEY="k",
            FIREBASE_AUTH_DOMAIN="p.firebaseapp.com",
            FIREBASE_APP_ID="a",
            DASHCAM_NOTIFY_DOT=True,
            # Note what is *not* here: no allowlist, no ALLOW_LIVE_FILING.
        )
        app = create_app(settings)
        app.dependency_overrides[require_user] = lambda: SOMEBODY
        app.dependency_overrides[require_mailable_user] = lambda: SOMEBODY

        with TestClient(app) as client:
            incident = save(client).json()

        assert incident["dot_state"] != "sent"
        assert incident["dot_destination"] is None
        # And it records *why*, rather than silently doing nothing.
        if incident["dot_state"] == "refused":
            assert "efus" in incident["dot_error"] or "SMTP" in incident["dot_error"]


class TestWhatItRefuses:
    def test_no_coordinates_means_no_report(self, client):
        """A crew cannot act on "there is debris somewhere"."""
        r = client.post(
            "/api/incidents",
            data={"meta": json.dumps({"hazard": "debris"})},
            files={"image": ("x.jpg", JPEG, "image/jpeg")},
        )
        assert r.status_code == 422
        assert "coordinates" in r.json()["detail"]

    def test_a_coordinate_in_the_sea_is_refused(self, client):
        r = save(client, lat=0.0, lng=0.0)
        assert r.status_code == 422

    def test_meta_that_is_not_json(self, client):
        r = client.post(
            "/api/incidents",
            data={"meta": "not json at all"},
            files={"image": ("x.jpg", JPEG, "image/jpeg")},
        )
        assert r.status_code == 422

    def test_an_empty_still(self, client):
        r = client.post(
            "/api/incidents",
            data={"meta": meta()},
            files={"image": ("x.jpg", b"", "image/jpeg")},
        )
        assert r.status_code == 422

    def test_an_oversized_upload(self, client):
        """The route stores what it is given, so it must bound it."""
        r = client.post(
            "/api/incidents",
            data={"meta": meta()},
            files={"image": ("x.jpg", b"\xff" * (3 * 1024 * 1024), "image/jpeg")},
        )
        assert r.status_code == 413

    def test_nothing_is_saved_when_it_refuses(self, client):
        """A refusal must not leave a half-record or an orphaned image."""
        save(client, lat=0.0, lng=0.0)
        assert client.get("/api/incidents").json()["incidents"] == []


class TestTheTwentyFourHourDuplicate:
    """One pothole, many drivers, one email.

    The check crosses users on purpose -- the second person to drive past a
    hazard is usually not the first -- so these tests report as two different
    people and assert that the second one is recognised as a duplicate.

    Nothing here can assert that mail was *not* sent, because SMTP is unset in
    this module and nothing sends either way. What it asserts instead is the
    decision: `dedup_reason` is set exactly when the mail was held, and
    `dot_state` says "duplicate" rather than blaming a setting.
    """

    ELSEWHERE = {"lat": 33.7760, "lng": -84.3880}  # ~3km north, outside 500m

    def as_somebody_else(self, app):
        other = AuthUser("uid-zzz999", "other@example.com", True, None, None)
        app.dependency_overrides[require_user] = lambda: other
        app.dependency_overrides[require_mailable_user] = lambda: other

    def test_the_first_report_goes_out(self, client):
        first = save(client).json()
        assert first["dedup_reason"] is None
        assert first["reports_24h"] == 1

    def test_the_second_is_held(self, client):
        save(client)
        second = save(client).json()

        assert second["dedup_reason"]
        assert second["reports_24h"] == 2
        # Not "held", which would blame DASHCAM_NOTIFY_DOT for a decision the
        # duplicate check made.
        assert second["dot_state"] == "duplicate"

    def test_it_is_still_saved_in_full(self, client):
        """The whole point: the reporter keeps their record either way."""
        save(client)
        second = save(client).json()

        stored = client.get("/api/incidents").json()["incidents"]
        assert second["id"] in [i["id"] for i in stored]
        assert second["body"]
        assert client.get(second["image_url"]).status_code == 200

    def test_somebody_elses_report_counts(self, settings):
        """A duplicate is a duplicate whoever filed the first one."""
        app = create_app(settings)
        app.dependency_overrides[require_user] = lambda: SOMEBODY
        app.dependency_overrides[require_mailable_user] = lambda: SOMEBODY
        with TestClient(app) as client:
            save(client)

        self.as_somebody_else(app)
        with TestClient(app) as client:
            second = save(client).json()

        assert second["reports_24h"] == 2
        assert second["dedup_reason"]

    def test_but_only_their_own_shows_on_their_page(self, settings):
        """Counting across users must not leak across them."""
        app = create_app(settings)
        app.dependency_overrides[require_user] = lambda: SOMEBODY
        app.dependency_overrides[require_mailable_user] = lambda: SOMEBODY
        with TestClient(app) as client:
            save(client)

        self.as_somebody_else(app)
        with TestClient(app) as client:
            save(client)
            listed = client.get("/api/incidents").json()["incidents"]

        assert len(listed) == 1

    def test_a_different_hazard_is_not_a_duplicate(self, client):
        save(client, hazard="debris")
        other = save(client, hazard="flooding").json()
        assert other["dedup_reason"] is None
        assert other["reports_24h"] == 1

    def test_the_same_hazard_elsewhere_is_not_a_duplicate(self, client):
        save(client)
        other = save(client, **self.ELSEWHERE).json()
        assert other["dedup_reason"] is None

    def test_the_reason_says_what_and_when(self, client):
        """It is the text on the card, so it has to read as an explanation."""
        save(client)
        reason = save(client).json()["dedup_reason"]
        assert "debris" in reason
        assert "ago" in reason

    def test_the_same_spot_does_not_report_a_distance_of_nought(self, client):
        """`format_distance` rounds to ten metres, so it would say "0 metres"."""
        save(client)
        assert "in the same spot" in save(client).json()["dedup_reason"]

    def test_a_real_distance_is_quoted(self, client):
        """Inside the radius, but far enough that the number means something."""
        save(client)
        # ~330m north: still a duplicate, but not the same spot.
        reason = save(client, lat=33.7520, lng=-84.3880).json()["dedup_reason"]
        assert "metres away" in reason


class TestOwnership:
    def test_one_persons_incident_is_not_anothers(self, settings):
        """The uid comes off the token, so the store is asked with theirs."""
        app = create_app(settings)
        app.dependency_overrides[require_user] = lambda: SOMEBODY
        app.dependency_overrides[require_mailable_user] = lambda: SOMEBODY

        with TestClient(app) as client:
            mine = save(client).json()

        somebody_else = AuthUser("uid-zzz999", "other@example.com", True, None, None)
        app.dependency_overrides[require_user] = lambda: somebody_else

        with TestClient(app) as client:
            assert client.get("/api/incidents").json()["incidents"] == []
            assert client.get(f"/api/incidents/{mine['id']}/image").status_code == 404

    def test_a_made_up_incident_id_is_a_404_not_a_500(self, client):
        assert client.get("/api/incidents/nope/image").status_code == 404
