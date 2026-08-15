-- Road Cleaner local schema.
--
-- Mirrors the Firestore collection layout closely enough that the two
-- repository adapters stay behaviourally interchangeable. JSON columns hold the
-- genuinely document-shaped fields (evidence lists, frame refs, model output)
-- rather than being normalised into tables nothing ever joins against.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cameras (
    id              TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    name            TEXT NOT NULL,
    road            TEXT NOT NULL,
    direction       TEXT,
    lat             REAL NOT NULL,
    lng             REAL NOT NULL,
    owner_agency_id TEXT,
    snapshot_url    TEXT NOT NULL,
    stream_url      TEXT,
    tier            TEXT NOT NULL DEFAULT 'quiet',
    active          INTEGER NOT NULL DEFAULT 1,
    county          TEXT,
    last_polled_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_cameras_state ON cameras(state);
-- The Watcher's hot query: which cameras are due for a look.
CREATE INDEX IF NOT EXISTS idx_cameras_due ON cameras(active, last_polled_at);

CREATE TABLE IF NOT EXISTS frames (
    id          TEXT PRIMARY KEY,
    camera_id   TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    blob_key    TEXT NOT NULL,
    phash       TEXT NOT NULL,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_frames_camera ON frames(camera_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS detections (
    id              TEXT PRIMARY KEY,
    camera_id       TEXT NOT NULL,
    frame_id        TEXT NOT NULL,
    analyzed_at     TEXT NOT NULL,
    hazard_type     TEXT NOT NULL,
    lane_position   TEXT NOT NULL,
    severity        TEXT NOT NULL,
    confidence      REAL NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    visual_evidence TEXT NOT NULL DEFAULT '[]',
    box             TEXT,
    raw_model_json  TEXT NOT NULL DEFAULT '{}',
    model_name      TEXT NOT NULL DEFAULT '',
    prefilter_passed INTEGER NOT NULL DEFAULT 1
);
-- Supports the gate's persistence check: recent detections for one camera.
CREATE INDEX IF NOT EXISTS idx_detections_camera ON detections(camera_id, analyzed_at DESC);

CREATE TABLE IF NOT EXISTS agencies (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    level             TEXT NOT NULL,
    state             TEXT NOT NULL,
    channel           TEXT NOT NULL,
    endpoint          TEXT,
    email             TEXT,
    ref_format        TEXT NOT NULL DEFAULT 'REF-#####',
    ref_label         TEXT,
    sla_overrides     TEXT NOT NULL DEFAULT '{}',
    jurisdiction_note TEXT
);

CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    correlation_key TEXT,
    camera_id       TEXT NOT NULL,
    state           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    hazard_type     TEXT NOT NULL,
    hazard_title    TEXT NOT NULL,
    location        TEXT NOT NULL,
    severity        TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0,
    opened_at       TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    closed_at       TEXT,
    gate_decision   TEXT NOT NULL,
    gate_reason     TEXT,
    agency_id       TEXT,
    agency_name     TEXT,
    channel         TEXT,
    reference       TEXT,
    ref_label       TEXT,
    sla_deadline    TEXT,
    escalation_tier INTEGER NOT NULL DEFAULT 0,
    sentence        TEXT NOT NULL DEFAULT '',
    explain         TEXT NOT NULL DEFAULT '',
    detection_ids   TEXT NOT NULL DEFAULT '[]',
    frame_refs      TEXT NOT NULL DEFAULT '[]',
    raw_model_json  TEXT NOT NULL DEFAULT '{}',
    box             TEXT,
    box_label       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);
CREATE INDEX IF NOT EXISTS idx_cases_kind ON cases(kind);
CREATE INDEX IF NOT EXISTS idx_cases_opened ON cases(opened_at DESC);
-- How the Analyst finds an already-open case instead of filing a duplicate.
CREATE INDEX IF NOT EXISTS idx_cases_correlation ON cases(correlation_key, kind);

CREATE TABLE IF NOT EXISTS trail_events (
    id      TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    at      TEXT NOT NULL,
    stage   TEXT NOT NULL,
    text    TEXT NOT NULL,
    tone    TEXT NOT NULL DEFAULT 'routine'
);
CREATE INDEX IF NOT EXISTS idx_trail_case ON trail_events(case_id, at);

CREATE TABLE IF NOT EXISTS filings (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL,
    agency_id    TEXT NOT NULL,
    channel      TEXT NOT NULL,
    tier         INTEGER NOT NULL DEFAULT 1,
    filed_at     TEXT NOT NULL,
    subject      TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    attachments  TEXT NOT NULL DEFAULT '[]',
    reference    TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 1,
    response_raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_filings_case ON filings(case_id, filed_at);

-- Per-state case number allocation, so ids read like 'GA-4471' rather than a uuid.
CREATE TABLE IF NOT EXISTS case_sequence (
    state TEXT PRIMARY KEY,
    next  INTEGER NOT NULL
);
