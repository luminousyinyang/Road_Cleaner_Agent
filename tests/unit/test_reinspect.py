"""Re-deriving a case from the footage attached to it.

The command's job is to make each case page true by construction: what the page
claims is what the model can actually see in the clip. Its other job, which
matters more, is to be safe -- it rewrites records in a database holding a demo
week that cannot be regenerated identically.

So the tests here are mostly about restraint: that nothing is written unless
asked, that a copy exists before anything is, and that the fields it does
rewrite are the ones the clip is evidence about and no others.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from road_cleaner.cli import _adopt, _most_common, _warn_before_rewriting
from road_cleaner.config import Settings
from road_cleaner.domain import narrative
from road_cleaner.domain.enums import CaseKind, GateDecision, HazardType, Severity
from road_cleaner.domain.models import BoundingBox, Case


def _frame(hazard, confidence, *, index=0, size=0.1, measured=True):
    return {
        "index": index,
        "at": float(index),
        "stamp": f"{index}.0s",
        "state": "found",
        "found": True,
        "hazard": hazard,
        "lane": "lane_1",
        "severity": "high",
        "confidence": confidence,
        "description": f"A {hazard} is in the road.",
        "box": {"x": 0.3, "y": 0.4, "width": size, "height": size},
        "box_measured": measured,
    }


class _Result:
    clip_url = "/media/synthetic/GA-0001/clip.mp4"
    model_name = "gemini-3.7-flash"

    def __init__(self, frames):
        self.frames = frames


@pytest.fixture
def a_case():
    return Case(
        id="GA-0001",
        camera_id="GDOT-CCTV-0447",
        state="GA",
        kind=CaseKind.FILED,
        hazard_type=HazardType.ANIMAL,
        hazard_title="Animal near the carriageway",
        location="I-285 westbound at Camp Creek Pkwy",
        severity=Severity.MEDIUM,
        confidence=0.71,
        gate_decision=GateDecision.FILE,
        gate_reason="two frames agree",
        agency_id="ga-dot-d7",
        agency_name="Georgia DOT — District 7",
        reference="TMC-11821",
        box=BoundingBox(x=0.1, y=0.6, width=0.2, height=0.18),
        box_label="animal · 0.71",
    )


class TestWhatTheClipMostlyShows:
    """One outlying frame must not redefine a case the others agree about."""

    def test_the_majority_wins(self):
        assert _most_common(["debris", "debris", "stalled_vehicle", "debris"]) == "debris"

    def test_a_single_frame_is_its_own_majority(self):
        assert _most_common(["flooding"]) == "flooding"


class TestAdopting:
    async def test_it_rewrites_what_the_clip_is_evidence_about(self, container, a_case):
        result = _Result([_frame("debris", 0.94, index=1), _frame("debris", 0.88, index=2)])

        await _adopt(container, a_case, result, result.frames, "debris", narrative)

        assert a_case.hazard_type is HazardType.DEBRIS
        assert a_case.confidence == pytest.approx(0.94)
        assert a_case.box.width == pytest.approx(0.1)
        assert "0.94" in a_case.box_label and "debris" in a_case.box_label
        # The headline comes from the same function that wrote it originally.
        assert a_case.hazard_title != "Animal near the carriageway"

    async def test_it_leaves_the_record_of_what_happened_alone(self, container, a_case):
        """The trail, the filing and the agency are history, not description.

        Rewriting them to match an analysis run months later would be
        falsifying the record rather than correcting a label.
        """
        result = _Result([_frame("debris", 0.94)])
        before = (a_case.agency_id, a_case.reference, a_case.gate_decision, a_case.kind)

        await _adopt(container, a_case, result, result.frames, "debris", narrative)

        assert (a_case.agency_id, a_case.reference, a_case.gate_decision, a_case.kind) == before

    async def test_it_takes_the_most_confident_frame_of_the_winning_hazard(
        self, container, a_case
    ):
        result = _Result([
            _frame("debris", 0.62, index=0),
            _frame("flooding", 0.99, index=1),   # a confident outlier, outvoted
            _frame("debris", 0.91, index=2),
        ])

        await _adopt(container, a_case, result, result.frames, "debris", narrative)

        assert a_case.hazard_type is HazardType.DEBRIS
        assert a_case.confidence == pytest.approx(0.91)

    async def test_the_new_raw_json_says_where_it_came_from(self, container, a_case):
        """The page shows this verbatim, so it has to be traceable."""
        import json

        result = _Result([_frame("debris", 0.94)])
        await _adopt(container, a_case, result, result.frames, "debris", narrative)

        raw = json.loads(a_case.raw_model_json)
        assert raw["source"] == "reinspect"
        assert raw["model"] == "gemini-3.7-flash"
        assert raw["clip"].endswith(".mp4")

    async def test_the_rewrite_is_persisted(self, container, a_case):
        await container.repository.save_case(a_case)
        result = _Result([_frame("debris", 0.94)])

        await _adopt(container, a_case, result, result.frames, "debris", narrative)

        stored = await container.repository.get_case(a_case.id)
        assert stored.hazard_type is HazardType.DEBRIS


class TestTheBackup:
    def test_a_copy_is_taken_before_anything_is_rewritten(self, tmp_path: Path):
        db = tmp_path / "road_cleaner.db"
        db.write_bytes(b"pretend-database")
        settings = Settings(
            ROAD_CLEANER_MODE="local", DATA_DIR=str(tmp_path), SQLITE_PATH=str(db)
        )

        _warn_before_rewriting(settings)

        copies = list(tmp_path.glob("road_cleaner.before-reinspect-*.db"))
        assert len(copies) == 1
        assert copies[0].read_bytes() == b"pretend-database"

    def test_no_database_yet_is_not_an_error(self, tmp_path: Path):
        settings = Settings(
            ROAD_CLEANER_MODE="local",
            DATA_DIR=str(tmp_path),
            SQLITE_PATH=str(tmp_path / "nothing.db"),
        )
        _warn_before_rewriting(settings)  # must not raise


class TestACachedRunIsStillARun:
    """`--adopt` used to do nothing on a case that had already been analysed.

    The cache check skipped the whole case before the adoption branch, so the
    flag silently no-opped on exactly the cases most likely to have one -- which
    is all of them, after the first sweep.
    """

    def test_the_cached_shim_exposes_what_adopt_reads(self):
        from road_cleaner.cli import _Cached

        cached = _Cached({
            "frames": [_frame("debris", 0.9)],
            "clip_url": "/media/synthetic/GA-0001/clip.mp4",
            "model_name": "gemini-3.7-flash",
        })
        assert cached.frames and cached.clip_url and cached.model_name

    def test_it_tolerates_a_cache_written_by_an_older_build(self):
        from road_cleaner.cli import _Cached

        cached = _Cached({})
        assert cached.frames == []
        assert cached.clip_url is None and cached.model_name is None

    async def test_adopting_from_a_cached_run_rewrites_the_case(self, container, a_case):
        """The point of the fix: no model call, same rewrite."""
        from road_cleaner.cli import _Cached

        cached = _Cached({
            "frames": [_frame("debris", 0.93), _frame("debris", 0.81, index=1)],
            "clip_url": "/media/synthetic/GA-0001/clip.mp4",
            "model_name": "gemini-3.7-flash",
        })
        await _adopt(container, a_case, cached, cached.frames, "debris", narrative)

        assert a_case.hazard_type is HazardType.DEBRIS
        assert a_case.confidence == pytest.approx(0.93)
