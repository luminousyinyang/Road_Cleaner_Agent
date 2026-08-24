"""The words the system puts in front of people.

Most of this module is prose generation and does not need guarding. What does is
the rule that a position never gets narrated -- because when it did, it did not
stop at the dashboard. A lane number the model guessed reached case headlines and
the `Location:` line of reports addressed to a state DOT, which is a maintenance
crew sent to the wrong part of the carriageway.

So these tests are the fence around that: whatever `lane_position` holds, none of
it may appear in a title, a paragraph or a report.
"""

from __future__ import annotations

import pytest

from road_cleaner.domain import narrative
from road_cleaner.domain.enums import GateDecision, HazardType, Severity
from road_cleaner.domain.models import Camera, Detection, GateResult

# Everything the field has ever plausibly held, including the values the prompt
# no longer offers -- old rows and cached replies still carry them.
POSITIONS = [
    "lane_1", "lane_2", "lane_3", "all_lanes",
    "left_shoulder", "right_shoulder", "median", "median_barrier",
    "intersection", "unknown", "",
]


@pytest.fixture
def camera():
    return Camera(
        id="GDOT-CCTV-0447",
        state="GA",
        name="Camp Creek Pkwy",
        road="I-285",
        direction="westbound",
        lat=33.6407,
        lng=-84.4277,
        snapshot_url="https://example.invalid/cam.jpg",
    )


def _detection(hazard=HazardType.DEBRIS, position="lane_2", confidence=0.94):
    return Detection(
        camera_id="GDOT-CCTV-0447",
        frame_id="f",
        hazard_type=hazard,
        lane_position=position,
        severity=Severity.HIGH,
        confidence=confidence,
        description="A shredded tyre tread is lying across the carriageway.",
        visual_evidence=["dark mass against light asphalt"],
    )


def _gate(decision=GateDecision.FILE):
    return GateResult(
        decision=decision,
        reason="two frames agree",
        mean_confidence=0.93,
        corroborating_ids=["a"],
        matched_distance_m=500.0,
    )


class TestTitles:
    @pytest.mark.parametrize("position", POSITIONS)
    @pytest.mark.parametrize("hazard", list(HazardType))
    def test_no_title_ever_mentions_a_lane_number(self, hazard, position):
        title = narrative.hazard_title(_detection(hazard, position))
        assert "lane 1" not in title
        assert "lane 2" not in title
        assert "lane 3" not in title

    @pytest.mark.parametrize("position", POSITIONS)
    def test_a_title_is_the_same_whatever_the_position(self, position):
        """The position is not part of what the case is called."""
        assert narrative.hazard_title(_detection(position=position)) == narrative.hazard_title(
            _detection(position="unknown")
        )

    def test_a_finished_sentence_is_not_extended(self):
        """Regression: the old append only knew how to strip " in a travel lane".

        Every other hazard got a lane bolted onto a complete phrase, so an animal
        detected in lane 2 was titled "Animal on the shoulder in lane 2" -- wrong
        about the lane and self-contradictory about the shoulder.
        """
        assert narrative.hazard_title(_detection(HazardType.ANIMAL, "lane_2")) == (
            "Animal on the shoulder"
        )
        assert narrative.hazard_title(_detection(HazardType.STALLED_VEHICLE, "lane_1")) == (
            "Stalled car on the shoulder"
        )

    def test_the_subject_line_inherits_the_clean_title(self):
        subject = narrative.report_subject(_detection(position="lane_3"), "I-285")
        assert "lane 3" not in subject
        assert "I-285" in subject


class TestTheParagraph:
    @pytest.mark.parametrize("position", POSITIONS)
    def test_explain_never_narrates_the_position(self, camera, position):
        text = narrative.explain(_detection(position=position), _gate(), camera)
        for phrase in ("lane 1", "lane 2", "lane 3", "shoulder on", "in the median"):
            assert phrase not in text

    def test_it_does_not_say_the_roadway_twice(self, camera):
        """The old phrasing paired "a dark object sitting in the roadway" with
        "on the roadway" for an unknown lane, giving "...in the roadway on the
        roadway on I-285."."""
        text = narrative.explain(_detection(position="unknown"), _gate(), camera)
        assert "roadway on the roadway" not in text
        assert "  " not in text, "a removed clause left a double space"

    def test_it_still_says_what_and_where(self, camera):
        text = narrative.explain(_detection(), _gate(), camera)
        assert "I-285" in text
        assert text.startswith("There's ")


WHERE = "I-285 westbound at Camp Creek Pkwy"


def _body(position="lane_2", **kwargs):
    return narrative.report_body(
        _detection(position=position), WHERE, "Sun Aug 23, 11:54 PM", **kwargs
    )


class TestTheReportToTheAgency:
    """This one is not cosmetic. It is read by somebody who then drives there."""

    @pytest.mark.parametrize("position", POSITIONS)
    def test_the_location_line_carries_no_lane_number(self, position):
        location = next(
            line for line in _body(position).splitlines() if line.startswith("Location:")
        )
        assert "lane" not in location.lower()

    def test_the_location_is_the_string_it_was_handed(self):
        """Not rebuilt from a camera.

        The body used to derive its own location while the maintenance form sent
        `case.location`, so one submission could name two different places.
        """
        location = next(
            line for line in _body().splitlines() if line.startswith("Location:")
        )
        assert location == f"Location: {WHERE}."

    def test_it_names_no_camera(self):
        """A dashcam has no camera id, and quoting one was never useful to a crew."""
        body = _body()
        assert "Camera:" not in body
        assert "GDOT-CCTV-0447" not in body

    def test_it_does_not_claim_a_second_confirmation(self):
        """"Confirmed present at" was the wall clock at filing time.

        On a follow-up sent days after the sighting it asserted an observation
        that never happened.
        """
        assert "Confirmed present" not in _body()

    def test_the_frame_is_stamped_once_and_plainly(self):
        body = _body()
        assert "Observed: Sun Aug 23, 11:54 PM" in body

    def test_the_rest_of_the_report_is_intact(self):
        body = _body()
        assert "Filed automatically by Road Cleaner." in body
        assert "shredded tyre tread" in body


class TestItOnlyClaimsWhatIsEnclosed:
    """The body used to promise attachments that were never sent.

    Two of the three channels post form fields and no files at all, and the email
    channel silently attaches nothing when the blob store is remote. Telling a
    maintenance desk that evidence is enclosed when it is not is the kind of
    small lie that costs a system its credibility on first contact.
    """

    def test_nothing_enclosed_means_nothing_claimed(self):
        body = _body(attachment_count=0)
        assert "attached" not in body.lower()

    def test_one_attachment_is_singular(self):
        assert "A still from the footage is attached" in _body(attachment_count=1)

    def test_several_attachments_are_counted(self):
        assert "3 stills from the footage are attached." in _body(attachment_count=3)

    def test_a_link_is_offered_when_there_is_one(self):
        body = _body(attachment_count=0, evidence_url="https://example.test/media/x.jpg")
        assert "https://example.test/media/x.jpg" in body
        # Still no false claim of an attachment alongside it.
        assert "is attached" not in body

    def test_a_link_and_an_attachment_can_coexist(self):
        body = _body(attachment_count=1, evidence_url="https://example.test/media/x.jpg")
        assert "is attached" in body
        assert "https://example.test/media/x.jpg" in body


class TestTheSubject:
    def test_it_takes_a_place_rather_than_a_camera(self):
        subject = narrative.report_subject(_detection(), "I-285")
        assert subject == "Road hazard: debris in a travel lane on I-285"

    def test_a_follow_up_says_which_notice_it_is(self):
        assert "second notice" in narrative.report_subject(_detection(), "I-285", tier=2)


class TestTheTimestamp:
    def test_one_rendering_for_everybody(self):
        """Three callers formatted this three ways, two with UTC and one without."""
        from datetime import UTC, datetime

        moment = datetime(2026, 8, 23, 23, 54, tzinfo=UTC)
        assert narrative.observed_at(moment) == "Sun Aug 23, 11:54 PM UTC"

    def test_a_naive_moment_does_not_leave_a_dangling_space(self):
        from datetime import datetime

        assert narrative.observed_at(datetime(2026, 8, 23, 23, 54)).endswith("PM")


def test_the_phrase_table_is_gone():
    """If it comes back, so does the bug. There is no honest use for it."""
    assert not hasattr(narrative, "LANE_PHRASES")
    assert not hasattr(narrative, "lane_phrase")
