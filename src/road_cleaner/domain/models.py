"""The things this system knows about.

These models are the contract between every layer: adapters build them, agents
reason over them, the repository persists them and the web serializer renders
them. They hold no presentation state -- no colours, no percentages, no
formatted timestamps. Those are computed at the edge, in `web/serializers.py`,
so that changing how a case looks never means a migration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from road_cleaner.domain.enums import (
    AgencyLevel,
    CameraTier,
    CaseKind,
    Channel,
    GateDecision,
    HazardType,
    Severity,
    Stage,
    Tone,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(BaseModel):
    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)


# --------------------------------------------------------------------- camera


class Camera(Base):
    """A public traffic camera we can pull a still from."""

    id: str
    state: str
    name: str
    road: str
    direction: str | None = None
    lat: float
    lng: float
    owner_agency_id: str | None = None
    snapshot_url: str
    stream_url: str | None = None
    tier: CameraTier = CameraTier.QUIET
    active: bool = True
    last_polled_at: datetime | None = None
    county: str | None = None

    @property
    def label(self) -> str:
        """How the camera identifies itself in the UI, e.g. 'GDOT-CCTV-0447 - CAMP CREEK PKWY'."""
        return f"{self.id} · {self.name.upper()}"


class Frame(Base):
    """One still pulled from one camera at one moment."""

    id: str = Field(default_factory=new_id)
    camera_id: str
    captured_at: datetime = Field(default_factory=utcnow)
    blob_key: str
    phash: str
    width: int = 0
    height: int = 0


class BoundingBox(Base):
    """Where in the frame the hazard is, as fractions of width/height.

    Fractions rather than pixels so the same box overlays a thumbnail and a
    full-size frame without rescaling.
    """

    x: float
    y: float
    width: float
    height: float


# ------------------------------------------------------------------ detection


class Detection(Base):
    """What the vision model said about one frame."""

    id: str = Field(default_factory=new_id)
    camera_id: str
    frame_id: str
    analyzed_at: datetime = Field(default_factory=utcnow)
    hazard_type: HazardType
    lane_position: str
    severity: Severity
    confidence: float
    description: str
    visual_evidence: list[str] = Field(default_factory=list)
    box: BoundingBox | None = None
    # The model's response exactly as it came back. Shown verbatim in the UI --
    # if we are going to file paperwork on a machine's say-so, the say-so is
    # part of the record.
    raw_model_json: str = "{}"
    model_name: str = "scripted"
    prefilter_passed: bool = True

    @property
    def box_label(self) -> str:
        """The caption on the detection box, e.g. 'debris · 0.94'."""
        return f"{self.hazard_type.value} · {self.confidence:.2f}"


class OfficialEvent(Base):
    """An incident the state's own 511 feed already knows about.

    The reason we bother fetching these is to *not* report things. If the DOT
    has already posted it, we have nothing to add.
    """

    id: str
    state: str
    event_type: str
    lat: float
    lng: float
    description: str = ""
    started_at: datetime | None = None
    active: bool = True
    source: str = ""


# --------------------------------------------------------------------- agency


class Agency(Base):
    """Whoever is responsible for fixing the road."""

    id: str
    name: str
    level: AgencyLevel
    state: str
    channel: Channel
    endpoint: str | None = None
    email: str | None = None
    # e.g. "TMC-#####" -- used to generate plausible references in dry run and
    # to sanity-check what a real agency hands back.
    ref_format: str = "REF-#####"
    ref_label: str | None = None
    sla_overrides: dict[str, int] = Field(default_factory=dict)
    jurisdiction_note: str | None = None

    @property
    def display_ref_label(self) -> str:
        return (self.ref_label or self.name).upper()


# ----------------------------------------------------------------------- case


class FrameRef(Base):
    """A frame attached to a case, with the role it plays in the story."""

    label: str
    captured_at: datetime | None = None
    blob_key: str | None = None
    mark: bool = False   # the key evidence frame
    clear: bool = False  # the frame that shows it resolved


class TrailEvent(Base):
    """One line of the audit trail: what the agent did, when, and why."""

    id: str = Field(default_factory=new_id)
    case_id: str
    at: datetime = Field(default_factory=utcnow)
    stage: Stage
    text: str
    tone: Tone = Tone.ROUTINE


class Filing(Base):
    """A report as it went out (or would have gone out, in dry run)."""

    id: str = Field(default_factory=new_id)
    case_id: str
    agency_id: str
    channel: Channel
    tier: int = 1
    filed_at: datetime = Field(default_factory=utcnow)
    subject: str = ""
    body: str = ""
    attachments: list[str] = Field(default_factory=list)
    reference: str | None = None
    dry_run: bool = True
    response_raw: str | None = None


class Case(Base):
    """A hazard we are doing something about.

    A case is opened the moment a detection survives the confidence floor, and
    it stays open -- being re-checked on a decaying schedule -- until the road
    is clear or a human closes it.
    """

    id: str  # "GA-4471": state, then a per-state sequence
    camera_id: str
    state: str
    kind: CaseKind = CaseKind.WATCHING
    hazard_type: HazardType
    hazard_title: str
    location: str
    severity: Severity = Severity.MEDIUM
    confidence: float = 0.0

    opened_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None

    gate_decision: GateDecision = GateDecision.WATCH
    gate_reason: str | None = None

    agency_id: str | None = None
    agency_name: str | None = None
    channel: Channel | None = None
    reference: str | None = None
    ref_label: str | None = None

    sla_deadline: datetime | None = None
    escalation_tier: int = 0

    # Re-check scheduling. `next_check_at` decays as a case ages, so a two-week
    # guardrail repair is not re-examined every minute -- but an overdue case is
    # checked harder, not less.
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = None
    checks_done: int = 0

    # Plain-English copy shown to a reader. Generated in `narrative.py` from
    # the detection, never written by hand.
    sentence: str = ""
    explain: str = ""

    detection_ids: list[str] = Field(default_factory=list)
    frame_refs: list[FrameRef] = Field(default_factory=list)
    raw_model_json: str = "{}"
    box: BoundingBox | None = None
    box_label: str = ""

    @property
    def is_open(self) -> bool:
        return self.kind in (CaseKind.WATCHING, CaseKind.FILED, CaseKind.ESCALATED)

    @property
    def was_filed(self) -> bool:
        """Did a report actually go to an agency?

        Keyed on `agency_id` rather than on `reference`, because a suppressed
        case carries the reference "duplicate" as a display label -- and
        treating that as evidence of a filing makes suppressed cases claim an
        agency they were never sent to.
        """
        return self.agency_id is not None

    @property
    def state_code(self) -> str:
        return self.id.split("-")[0]

    @property
    def number(self) -> str:
        parts = self.id.split("-", 1)
        return parts[1] if len(parts) > 1 else self.id


class CaseWithDetail(Base):
    """A case plus everything the detail page needs, assembled by the repository."""

    case: Case
    camera: Camera | None = None
    agency: Agency | None = None
    trail: list[TrailEvent] = Field(default_factory=list)
    filings: list[Filing] = Field(default_factory=list)
    detections: list[Detection] = Field(default_factory=list)


# --------------------------------------------------------------------- events


class FrameCaptured(Base):
    """Bus payload: the Watcher saw something worth a closer look."""

    frame: Frame
    camera: Camera


class HazardConfirmed(Base):
    """Bus payload: the Analyst is confident enough to want this reported."""

    case_id: str
    detection: Detection
    camera: Camera


class GateResult(Base):
    """The confidence gate's verdict on a detection.

    Carries its reasoning, not just its answer, because the reason is what ends
    up on the case trail and in front of a human.
    """

    decision: GateDecision
    reason: str
    mean_confidence: float = 0.0
    corroborating_ids: list[str] = Field(default_factory=list)
    matched_event: OfficialEvent | None = None
    matched_distance_m: float | None = None
