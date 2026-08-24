"""The drill: describe a hazard, run the real pipeline, stop before sending.

These run entirely offline. The scaffold model and the vision model are both
replaced with fakes, because what is worth testing here is not that Gemma and
Gemini work -- it is that the drill wires the *real* gate, the *real* jurisdiction
lookup and the *real* report composition together, and that the result can never
be filed.
"""

from __future__ import annotations

import pytest

from road_cleaner.domain.enums import HazardType, Severity
from road_cleaner.domain.models import Detection
from road_cleaner.pipeline.drill import STAGES, Drill, DrillError, _parse_json

SPEC = {
    "state": "GA",
    "road": "I-85",
    "direction": "northbound",
    "county": "Fulton",
    "place": "spaghetti junction",
    "hazard_type": "debris",
    "lane_position": "lane_1",
    "severity": "high",
    "description": "A mattress is lying across the left travel lane.",
}


class FakeVision:
    """Returns a confident detection for whatever frame it is handed."""

    model_name = "fake-vision"

    def __init__(self) -> None:
        self.calls = 0

    async def prefilter(self, image, frame, camera):
        return True

    async def analyze(self, image, frame, camera):
        self.calls += 1
        return Detection(
            camera_id=camera.id,
            frame_id=frame.id,
            analyzed_at=frame.captured_at,
            hazard_type=HazardType.DEBRIS,
            lane_position="lane_1",
            severity=Severity.HIGH,
            confidence=0.91,
            description="A mattress is lying across the left travel lane.",
            model_name=self.model_name,
        )


@pytest.fixture
def drill(container, monkeypatch):
    container.vision = FakeVision()
    d = Drill(container)

    async def fake_scaffold(text):
        return dict(SPEC)

    monkeypatch.setattr(d, "_scaffold", fake_scaffold)
    return d


# ------------------------------------------------------------------- parsing


def test_scaffold_json_survives_a_markdown_fence():
    """Gemma fences its JSON regardless of what the prompt asks for."""
    assert _parse_json('```json\n{"state":"GA"}\n```') == {"state": "GA"}
    assert _parse_json('Sure! {"state":"FL"} hope that helps') == {"state": "FL"}
    assert _parse_json("not json at all") is None
    assert _parse_json("") is None


# -------------------------------------------------------------------- shape


def test_there_is_no_push_stage():
    """A drill has six stages. Showing a greyed-out seventh would suggest it
    ran out of road rather than declined on purpose."""
    assert [k for k, _ in STAGES] == [
        "scaffold", "stage", "detect", "confirm", "resolve", "report",
    ]


async def test_empty_input_is_refused(drill):
    with pytest.raises(DrillError, match="Describe a hazard"):
        await drill.run("   ")


# ------------------------------------------------------------------ the run


async def test_a_drill_runs_the_whole_pipeline_and_stops(drill, container):
    result = await drill.run("a mattress in the fast lane on I-85")

    assert [s.state for s in result.stages] == ["done"] * 6
    assert result.case_id.startswith("SIM-")
    assert result.filed is False
    assert result.report_body and result.report_subject

    case = await container.repository.get_case(result.case_id)
    assert case.synthetic is True


async def test_both_frames_are_analysed_independently(drill, container):
    """The gate needs two observations. Two frames, two model calls -- not one
    call and a fabricated second row."""
    result = await drill.run("a mattress in the fast lane on I-85")

    assert container.vision.calls == 2
    assert len(result.detections) == 2
    assert len({d["at"] for d in result.detections}) == 2, "the two frames share a timestamp"
    assert len(result.frame_urls) == 2


async def test_the_gate_actually_runs(drill):
    """Not a rubber stamp: the real `domain.gating.evaluate` decides."""
    result = await drill.run("a mattress in the fast lane on I-85")
    assert result.gate_decision in {"file", "watch", "suppress", "drop"}
    assert result.gate_reason


async def test_the_invented_camera_has_no_owner_so_adk_must_resolve(drill, container):
    """A camera that does not exist has no known owner. That is what stops the
    jurisdiction rules shortcutting to `use_camera_owner` and forces the
    reasoner to work the answer out."""
    result = await drill.run("a mattress in the fast lane on I-85")
    case = await container.repository.get_case(result.case_id)
    camera = await container.repository.get_camera(case.camera_id)
    assert camera.owner_agency_id is None


# ------------------------------------------------------------- the boundary


async def test_a_drill_case_stays_out_of_the_road_log(drill, container):
    result = await drill.run("a mattress in the fast lane on I-85")

    visible = await container.repository.list_cases(limit=100)
    assert result.case_id not in [c.id for c in visible]
    assert result.case_id not in [c.id for c in await container.repository.open_cases()]


async def test_a_drill_writes_nothing_to_the_outbox(drill, container, settings):
    from pathlib import Path

    outbox = Path(settings.filing_outbox)
    before = set(outbox.glob("*")) if outbox.exists() else set()

    await drill.run("a mattress in the fast lane on I-85")

    after = set(outbox.glob("*")) if outbox.exists() else set()
    assert after == before, "composing a report must not transmit or spool one"


async def test_drill_frames_are_not_served_as_camera_evidence(drill, container):
    """They live in the media store, not the evidence store behind /frames/."""
    from road_cleaner.ports.blob_store import BlobNotFoundError

    result = await drill.run("a mattress in the fast lane on I-85")
    case = await container.repository.get_case(result.case_id)

    for ref in case.frame_refs:
        with pytest.raises(BlobNotFoundError):
            await container.blobs.get(ref.blob_key)
