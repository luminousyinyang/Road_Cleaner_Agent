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
    # A coarse position -- `intersection`, a shoulder, the median, or `unknown`.
    # Never a lane number: see `narrative` for why, and `vision.POSITIONS` for
    # the vocabulary. Read by jurisdiction routing and by nothing else.
    lane_position: str
    severity: Severity
    confidence: float
    description: str
    visual_evidence: list[str] = Field(default_factory=list)
    box: BoundingBox | None = None
    # True when `box` is coordinates somebody actually measured -- either the
    # vision model returned them, or (in the scripted analyzer) we drew the
    # hazard there ourselves and know the geometry. False means the box is an
    # approximation and the UI draws it dashed.
    #
    # The lane-name lookup table this once guarded against is gone: a model that
    # returns no box now yields no box, because a rectangle placed from a field
    # nobody trusts is the same class of lie as a fabricated case reference.
    box_is_measured: bool = False
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

    # True when the case came from a drill rather than from a camera: an invented
    # location, generated footage, a real pipeline run over it.
    #
    # This is the load-bearing flag in the whole system. A synthetic case may
    # never be filed, never appears in the road log or the public statistics, and
    # is badged wherever it is shown. `Dispatcher._file_locked` refuses one
    # outright rather than trusting callers to check. The parallel for media is
    # `ports.media.is_synthetic_key`; this extends the same boundary to cases.
    synthetic: bool = False
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


# ------------------------------------------------------------------- incidents


class IncidentSighting(Base):
    """The four facts about a saved incident that the 24h dedup check needs.

    Deliberately not an `Incident`. The dedup check is the one read in this
    system that crosses users -- two strangers driving the same road an hour
    apart is exactly the case it exists to catch -- and a cross-user read that
    returned whole incidents would be a route to somebody else's photograph,
    their description, their agency correspondence and the address it was
    mailed to.

    So the store projects down to this on the way out. What a caller cannot
    load, a caller cannot leak, and there is nothing here that identifies a
    person: no uid, no incident id, no prose. Just what kind of hazard, where,
    and when -- which is the whole of what `find_recent_similar` reasons over.
    """

    hazard_type: HazardType
    lat: float
    lng: float
    created_at: datetime


class Incident(Base):
    """Something a signed-in person saw through their own dashcam, and kept.

    Deliberately *not* a `Case`. A Case is the traffic-camera pipeline's unit of
    work: it has a sequenced public id, it counts toward the statistics on the
    front page, it appears in the scenario library, and the Auditor keeps
    re-checking it until the road is clear. None of that is true of a phone
    pointed at a road for ninety seconds, and letting one become the other would
    put user-submitted findings into the numbers this project reports about
    itself.

    So this is its own record, owned by a `uid`, holding the thing the person
    actually wants back later: the picture with the box burned into it, where
    they were, what the model said, which agency owns that road, and what was
    done about it. Written only when somebody presses the button -- a find that
    times out unreported leaves nothing behind at all.
    """

    id: str = Field(default_factory=new_id)
    uid: str
    created_at: datetime = Field(default_factory=utcnow)

    # --- what the model saw
    hazard_type: HazardType
    hazard_label: str = ""
    severity: Severity
    confidence: float
    description: str = ""
    box: BoundingBox | None = None
    box_is_measured: bool = False
    model_name: str = ""

    # --- where
    lat: float
    lng: float
    location: str = ""
    state: str = ""

    # --- whose road it is. Resolved through the same jurisdiction registry the
    # rest of the system uses, so a saved incident names the same agency a real
    # case at that coordinate would have named.
    agency_id: str | None = None
    agency_name: str | None = None
    agency_email: str | None = None
    agency_endpoint: str | None = None
    channel: Channel | None = None
    rule_id: str | None = None

    # --- the report, as sent
    report_subject: str = ""
    report_body: str = ""

    # --- what actually happened to it. Two separate facts on purpose: the copy
    # to the reporter is the normal path, and the copy to the agency is the one
    # that needs DASHCAM_NOTIFY_DOT *and* an address that clears guard_live_send.
    # A single "sent" boolean could not tell you which of those occurred.
    emailed_to: str | None = None
    emailed_at: datetime | None = None
    dot_notified: bool = False
    dot_destination: str | None = None
    dot_error: str | None = None

    # --- what the 24h dedup check found, recorded as of the moment this was
    # saved rather than recomputed on read. The count is why no mail went out,
    # so it has to be the number the decision was actually made on; a figure
    # that drifted as the window rolled would stop explaining the record it is
    # attached to. Counts *other* reports, so 0 means this was the first.
    similar_recent_count: int = 0
    # Set only when that count held the mail back. Doubles as the flag -- a
    # separate boolean could disagree with the sentence next to it.
    dedup_reason: str | None = None

    # Blob store keys, not URLs. The store may be local disk or GCS, and a URL
    # baked in at write time would be wrong the moment the deployment moves.
    image_keys: list[str] = Field(default_factory=list)
