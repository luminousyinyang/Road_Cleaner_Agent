"""Looking at a picture of a road and saying what is wrong with it."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from road_cleaner.domain.models import Camera, Detection, Frame


class VisionUnavailableError(RuntimeError):
    """The model could not be reached or gave an unusable answer.

    Part of the port's contract rather than any one adapter's business: every
    caller has to be able to tell "I could not look" apart from "I looked and
    the road was clear", and every implementation owes them that distinction.
    Raised, never swallowed.
    """


class ClearanceCheck:
    """Whether a previously-reported hazard is still in the frame.

    Deliberately a separate question from "what hazards are in this image".
    Asking "is *this specific thing* still there, compared to this evidence
    photo" is a much easier question to answer reliably, and it is the one the
    Auditor needs to close a case with a credible before/after pair.
    """

    def __init__(self, still_present: bool, confidence: float, note: str = "") -> None:
        self.still_present = still_present
        self.confidence = confidence
        self.note = note

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ClearanceCheck(still_present={self.still_present}, "
            f"confidence={self.confidence:.2f}, note={self.note!r})"
        )


@runtime_checkable
class VisionAnalyzer(Protocol):
    """Vision over road scenes.

    Two implementations: Gemini on Vertex AI, and a deterministic scripted
    analyzer that replays a scenario file so the pipeline runs without a key.
    """

    @property
    def model_name(self) -> str:
        """What to record on the detection, so evidence says which model judged it."""
        ...

    async def prefilter(self, image: bytes, frame: Frame, camera: Camera) -> bool:
        """Cheap yes/no: is anything anomalous here at all?

        Most frames of a road are an empty road. This kills the majority of them
        for near-zero cost before the expensive model ever sees one.

        The contract that matters is **high recall, not high precision**. Letting
        an ordinary frame through wastes a fraction of a cent; discarding a frame
        with a hazard in it loses the hazard entirely, and no later stage can
        recover it. Implementations should err heavily toward passing.

        Returning True always is a valid implementation. It just costs more.
        """
        ...

    async def analyze(self, image: bytes, frame: Frame, camera: Camera) -> Detection | None:
        """Full structured analysis. None means nothing worth reporting."""
        ...

    async def verify_cleared(
        self, image: bytes, evidence_image: bytes, detection: Detection, camera: Camera
    ) -> ClearanceCheck:
        """Compare a fresh frame against the original evidence: is it still there?"""
        ...
