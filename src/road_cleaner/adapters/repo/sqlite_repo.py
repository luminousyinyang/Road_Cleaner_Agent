"""SQLite-backed repository. The default store for local runs.

Uses stdlib `sqlite3` on a worker thread rather than pulling in an async driver:
the query volume here is tiny (a few hundred rows a minute at full tilt) and one
fewer dependency is worth more than the throughput we would gain.

A single connection guarded by a lock keeps writes serialised, which SQLite
wants anyway, and WAL mode keeps the dashboard's reads from blocking behind the
Watcher's writes.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from road_cleaner.domain.enums import CameraTier, CaseKind, GateDecision
from road_cleaner.domain.models import (
    Agency,
    BoundingBox,
    Camera,
    Case,
    CaseWithDetail,
    Detection,
    Filing,
    Frame,
    FrameRef,
    TrailEvent,
)
from road_cleaner.logging import get_logger

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Starting case numbers per state, chosen so demo ids look like a system that
# has been running a while rather than one that started this morning.
SEQUENCE_START = {"GA": 4460, "FL": 2195, "NC": 1168}


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` leaves an
# existing table alone, so a database created before a column existed would keep
# failing on every read until it was deleted -- and `data/road_cleaner.db` holds
# the demo week, which is not something to throw away over one column.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "cases": [("synthetic", "INTEGER NOT NULL DEFAULT 0")],
    "detections": [("box_is_measured", "INTEGER NOT NULL DEFAULT 0")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any columns this build expects that an older database lacks."""
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, spec in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


class SqliteCaseRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_PATH.read_text())
        _migrate(conn)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        """Checkpoint the WAL into the database, then close.

        WAL mode means committed writes can still be sitting in the -wal
        sidecar. That is fine while the process lives, but a run that produces
        a week of cases and then exits must leave them readable by the *next*
        process -- the dashboard is a separate process from the pipeline that
        populated it. TRUNCATE folds the WAL back into the main file and resets
        it, so what is on disk afterwards is the whole story.
        """
        if self._conn is None:
            return

        def finish() -> None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as exc:  # pragma: no cover - best effort
                log.warning("WAL checkpoint failed on close", extra={"error": str(exc)})
            self._conn.close()

        await asyncio.to_thread(finish)
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Repository not initialized -- call initialize() first")
        return self._conn

    async def _write(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            def run() -> None:
                self.conn.execute(sql, params)
                self.conn.commit()

            await asyncio.to_thread(run)

    async def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            return self.conn.execute(sql, params).fetchall()

        return await asyncio.to_thread(run)

    async def _read_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = await self._read(sql, params)
        return rows[0] if rows else None

    # ---------------------------------------------------------------- cameras
    async def upsert_camera(self, camera: Camera) -> None:
        await self._write(
            """INSERT INTO cameras (id, state, name, road, direction, lat, lng,
                   owner_agency_id, snapshot_url, stream_url, tier, active, county,
                   last_polled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   state=excluded.state, name=excluded.name, road=excluded.road,
                   direction=excluded.direction, lat=excluded.lat, lng=excluded.lng,
                   owner_agency_id=excluded.owner_agency_id,
                   snapshot_url=excluded.snapshot_url, stream_url=excluded.stream_url,
                   tier=excluded.tier, active=excluded.active, county=excluded.county,
                   last_polled_at=excluded.last_polled_at""",
            (
                camera.id, camera.state, camera.name, camera.road, camera.direction,
                camera.lat, camera.lng, camera.owner_agency_id, camera.snapshot_url,
                camera.stream_url, camera.tier.value, int(camera.active), camera.county,
                _iso(camera.last_polled_at),
            ),
        )

    def _camera(self, row: sqlite3.Row) -> Camera:
        return Camera(
            id=row["id"], state=row["state"], name=row["name"], road=row["road"],
            direction=row["direction"], lat=row["lat"], lng=row["lng"],
            owner_agency_id=row["owner_agency_id"], snapshot_url=row["snapshot_url"],
            stream_url=row["stream_url"], tier=CameraTier(row["tier"]),
            active=bool(row["active"]), county=row["county"],
            last_polled_at=_dt(row["last_polled_at"]),
        )

    async def get_camera(self, camera_id: str) -> Camera | None:
        row = await self._read_one("SELECT * FROM cameras WHERE id = ?", (camera_id,))
        return self._camera(row) if row else None

    async def list_cameras(self, state: str | None = None) -> list[Camera]:
        if state:
            rows = await self._read("SELECT * FROM cameras WHERE state = ? ORDER BY id", (state,))
        else:
            rows = await self._read("SELECT * FROM cameras ORDER BY id")
        return [self._camera(r) for r in rows]

    async def cameras_due(self, now: datetime, limit: int = 50) -> list[Camera]:
        """Cameras whose tier says they are due.

        Done in SQL rather than by loading the fleet and filtering in Python,
        because at 2,000+ cameras that difference starts to matter.
        """
        rows = await self._read(
            """SELECT * FROM cameras
               WHERE active = 1
                 AND (last_polled_at IS NULL OR last_polled_at <= ?)
               ORDER BY (last_polled_at IS NULL) DESC, last_polled_at ASC
               LIMIT ?""",
            (now.isoformat(), limit),
        )
        return [self._camera(r) for r in rows]

    # ----------------------------------------------------------------- frames
    async def save_frame(self, frame: Frame) -> None:
        await self._write(
            """INSERT OR REPLACE INTO frames
               (id, camera_id, captured_at, blob_key, phash, width, height)
               VALUES (?,?,?,?,?,?,?)""",
            (
                frame.id, frame.camera_id, frame.captured_at.isoformat(),
                frame.blob_key, frame.phash, frame.width, frame.height,
            ),
        )

    async def latest_frame(self, camera_id: str) -> Frame | None:
        row = await self._read_one(
            "SELECT * FROM frames WHERE camera_id = ? ORDER BY captured_at DESC LIMIT 1",
            (camera_id,),
        )
        if not row:
            return None
        return Frame(
            id=row["id"], camera_id=row["camera_id"], captured_at=_dt(row["captured_at"]),
            blob_key=row["blob_key"], phash=row["phash"],
            width=row["width"], height=row["height"],
        )

    # ------------------------------------------------------------- detections
    async def save_detection(self, detection: Detection) -> None:
        await self._write(
            """INSERT OR REPLACE INTO detections
               (id, camera_id, frame_id, analyzed_at, hazard_type, lane_position,
                severity, confidence, description, visual_evidence, box,
                raw_model_json, model_name, prefilter_passed, box_is_measured)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                detection.id, detection.camera_id, detection.frame_id,
                detection.analyzed_at.isoformat(), detection.hazard_type.value,
                detection.lane_position, detection.severity.value, detection.confidence,
                detection.description, json.dumps(detection.visual_evidence),
                detection.box.model_dump_json() if detection.box else None,
                detection.raw_model_json, detection.model_name,
                int(detection.prefilter_passed),
                int(detection.box_is_measured),
            ),
        )

    def _detection(self, row: sqlite3.Row) -> Detection:
        return Detection(
            id=row["id"], camera_id=row["camera_id"], frame_id=row["frame_id"],
            analyzed_at=_dt(row["analyzed_at"]), hazard_type=row["hazard_type"],
            lane_position=row["lane_position"], severity=row["severity"],
            confidence=row["confidence"], description=row["description"],
            visual_evidence=json.loads(row["visual_evidence"]),
            box=BoundingBox(**json.loads(row["box"])) if row["box"] else None,
            box_is_measured=bool(row["box_is_measured"]),
            raw_model_json=row["raw_model_json"], model_name=row["model_name"],
            prefilter_passed=bool(row["prefilter_passed"]),
        )

    async def recent_detections(
        self, camera_id: str, since: datetime, limit: int = 20
    ) -> list[Detection]:
        rows = await self._read(
            """SELECT * FROM detections
               WHERE camera_id = ? AND analyzed_at >= ?
               ORDER BY analyzed_at DESC LIMIT ?""",
            (camera_id, since.isoformat(), limit),
        )
        return [self._detection(r) for r in rows]

    # ------------------------------------------------------------------ cases
    async def save_case(self, case: Case) -> None:
        await self._write(
            """INSERT INTO cases
               (id, camera_id, state, kind, hazard_type, hazard_title, location,
                severity, confidence, opened_at, updated_at, closed_at, gate_decision,
                gate_reason, agency_id, agency_name, channel, reference, ref_label,
                sla_deadline, escalation_tier, last_checked_at, next_check_at,
                checks_done, sentence, explain, detection_ids,
                frame_refs, raw_model_json, box, box_label, synthetic)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   kind=excluded.kind, severity=excluded.severity,
                   confidence=excluded.confidence, updated_at=excluded.updated_at,
                   closed_at=excluded.closed_at, gate_decision=excluded.gate_decision,
                   gate_reason=excluded.gate_reason, agency_id=excluded.agency_id,
                   agency_name=excluded.agency_name, channel=excluded.channel,
                   reference=excluded.reference, ref_label=excluded.ref_label,
                   sla_deadline=excluded.sla_deadline,
                   escalation_tier=excluded.escalation_tier,
                   last_checked_at=excluded.last_checked_at,
                   next_check_at=excluded.next_check_at,
                   checks_done=excluded.checks_done, sentence=excluded.sentence,
                   explain=excluded.explain, detection_ids=excluded.detection_ids,
                   frame_refs=excluded.frame_refs, raw_model_json=excluded.raw_model_json,
                   box=excluded.box, box_label=excluded.box_label,
                   -- `hazard_title` was updated here while `hazard_type` was
                   -- not, so a re-saved case could end up titled "Debris in a
                   -- travel lane" while still typed `animal` -- the badge and
                   -- the headline on the same page disagreeing about what the
                   -- case is. They describe the same thing and move together.
                   hazard_title=excluded.hazard_title,
                   hazard_type=excluded.hazard_type""",
            (
                case.id, case.camera_id, case.state, case.kind.value,
                case.hazard_type.value, case.hazard_title, case.location,
                case.severity.value, case.confidence, case.opened_at.isoformat(),
                case.updated_at.isoformat(), _iso(case.closed_at),
                case.gate_decision.value, case.gate_reason, case.agency_id,
                case.agency_name, case.channel.value if case.channel else None,
                case.reference, case.ref_label, _iso(case.sla_deadline),
                case.escalation_tier, _iso(case.last_checked_at),
                _iso(case.next_check_at), case.checks_done,
                case.sentence, case.explain,
                json.dumps(case.detection_ids),
                json.dumps([json.loads(f.model_dump_json()) for f in case.frame_refs]),
                case.raw_model_json,
                case.box.model_dump_json() if case.box else None, case.box_label,
                int(case.synthetic),
            ),
        )

    def _case(self, row: sqlite3.Row) -> Case:
        return Case(
            id=row["id"], camera_id=row["camera_id"], state=row["state"],
            kind=CaseKind(row["kind"]), hazard_type=row["hazard_type"],
            hazard_title=row["hazard_title"], location=row["location"],
            severity=row["severity"], confidence=row["confidence"],
            opened_at=_dt(row["opened_at"]), updated_at=_dt(row["updated_at"]),
            closed_at=_dt(row["closed_at"]),
            gate_decision=GateDecision(row["gate_decision"]),
            gate_reason=row["gate_reason"], agency_id=row["agency_id"],
            agency_name=row["agency_name"], channel=row["channel"],
            reference=row["reference"], ref_label=row["ref_label"],
            sla_deadline=_dt(row["sla_deadline"]),
            escalation_tier=row["escalation_tier"],
            last_checked_at=_dt(row["last_checked_at"]),
            next_check_at=_dt(row["next_check_at"]),
            checks_done=row["checks_done"],
            sentence=row["sentence"],
            explain=row["explain"], detection_ids=json.loads(row["detection_ids"]),
            frame_refs=[FrameRef(**f) for f in json.loads(row["frame_refs"])],
            raw_model_json=row["raw_model_json"],
            box=BoundingBox(**json.loads(row["box"])) if row["box"] else None,
            box_label=row["box_label"],
            synthetic=bool(row["synthetic"]),
        )

    async def get_case(self, case_id: str) -> Case | None:
        row = await self._read_one("SELECT * FROM cases WHERE id = ?", (case_id,))
        return self._case(row) if row else None

    async def get_case_detail(self, case_id: str) -> CaseWithDetail | None:
        case = await self.get_case(case_id)
        if case is None:
            return None
        detection_rows = (
            await self._read(
                f"SELECT * FROM detections WHERE id IN "  # noqa: S608 - ids are internal
                f"({','.join('?' * len(case.detection_ids))}) ORDER BY analyzed_at",
                tuple(case.detection_ids),
            )
            if case.detection_ids
            else []
        )
        return CaseWithDetail(
            case=case,
            camera=await self.get_camera(case.camera_id),
            agency=await self.get_agency(case.agency_id) if case.agency_id else None,
            trail=await self.get_trail(case_id),
            filings=await self.get_filings(case_id),
            detections=[self._detection(r) for r in detection_rows],
        )

    async def find_open_case(self, correlation_key: str) -> Case | None:
        row = await self._read_one(
            """SELECT * FROM cases
               WHERE correlation_key = ? AND kind IN ('watching','filed','escalated')
               ORDER BY opened_at DESC LIMIT 1""",
            (correlation_key,),
        )
        return self._case(row) if row else None

    async def find_recent_case(self, correlation_key: str, since: datetime) -> Case | None:
        """The case for this camera+hazard, open or recently closed.

        Closed cases have to be findable too. A suppressed hazard is still on the
        road being detected every couple of minutes, and a hazard that just
        cleared may flicker back for a poll or two. If only open cases matched,
        each of those re-detections would allocate a brand new case id and the
        road log would fill with hundreds of copies of one problem.

        Open cases win over closed ones of the same age.
        """
        row = await self._read_one(
            """SELECT * FROM cases
               WHERE correlation_key = ?
                 AND (kind IN ('watching','filed','escalated') OR updated_at >= ?)
               ORDER BY
                 CASE WHEN kind IN ('watching','filed','escalated') THEN 0 ELSE 1 END,
                 updated_at DESC
               LIMIT 1""",
            (correlation_key, since.isoformat()),
        )
        return self._case(row) if row else None

    async def set_correlation(self, case_id: str, correlation_key: str) -> None:
        await self._write(
            "UPDATE cases SET correlation_key = ? WHERE id = ?", (correlation_key, case_id)
        )

    async def list_cases(
        self,
        state: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        include_synthetic: bool = False,
    ) -> list[Case]:
        """Cases, newest first.

        Drill cases are excluded unless asked for. Defaulting to exclusion is the
        point: every existing caller -- the road log, the statistics, the API --
        gets the safe answer without having to remember to filter, and only the
        drill surface opts in.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if not include_synthetic:
            clauses.append("synthetic = 0")
        if state and state != "all":
            clauses.append("state = ?")
            params.append(state)
        if kind and kind != "all":
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = await self._read(
            f"SELECT * FROM cases {where} ORDER BY opened_at DESC LIMIT ?",  # noqa: S608
            tuple(params),
        )
        return [self._case(r) for r in rows]

    async def open_cases(self) -> list[Case]:
        # Synthetic cases are never re-checked: the Auditor would go looking for
        # a camera that does not exist. They are a snapshot of one pipeline run,
        # not something with a life of its own.
        rows = await self._read(
            """SELECT * FROM cases
               WHERE kind IN ('watching','filed','escalated') AND synthetic = 0
               ORDER BY opened_at"""
        )
        return [self._case(r) for r in rows]

    async def next_case_id(self, state: str) -> str:
        """Allocate the next id for a state, atomically."""
        async with self._lock:
            def run() -> str:
                cur = self.conn.execute(
                    "SELECT next FROM case_sequence WHERE state = ?", (state,)
                ).fetchone()
                nxt = cur["next"] if cur else SEQUENCE_START.get(state, 1000)
                self.conn.execute(
                    "INSERT INTO case_sequence (state, next) VALUES (?,?) "
                    "ON CONFLICT(state) DO UPDATE SET next = excluded.next",
                    (state, nxt + 1),
                )
                self.conn.commit()
                return f"{state}-{nxt}"

            return await asyncio.to_thread(run)

    # ------------------------------------------------------------------ trail
    async def append_trail(self, event: TrailEvent) -> None:
        await self._write(
            "INSERT OR REPLACE INTO trail_events (id, case_id, at, stage, text, tone) "
            "VALUES (?,?,?,?,?,?)",
            (
                event.id, event.case_id, event.at.isoformat(),
                event.stage.value, event.text, event.tone.value,
            ),
        )

    async def get_trail(self, case_id: str) -> list[TrailEvent]:
        rows = await self._read(
            "SELECT * FROM trail_events WHERE case_id = ? ORDER BY at", (case_id,)
        )
        return [
            TrailEvent(
                id=r["id"], case_id=r["case_id"], at=_dt(r["at"]),
                stage=r["stage"], text=r["text"], tone=r["tone"],
            )
            for r in rows
        ]

    # ---------------------------------------------------------------- filings
    async def save_filing(self, filing: Filing) -> None:
        await self._write(
            """INSERT OR REPLACE INTO filings
               (id, case_id, agency_id, channel, tier, filed_at, subject, body,
                attachments, reference, dry_run, response_raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                filing.id, filing.case_id, filing.agency_id, filing.channel.value,
                filing.tier, filing.filed_at.isoformat(), filing.subject, filing.body,
                json.dumps(filing.attachments), filing.reference,
                int(filing.dry_run), filing.response_raw,
            ),
        )

    async def get_filings(self, case_id: str) -> list[Filing]:
        rows = await self._read(
            "SELECT * FROM filings WHERE case_id = ? ORDER BY filed_at", (case_id,)
        )
        return [
            Filing(
                id=r["id"], case_id=r["case_id"], agency_id=r["agency_id"],
                channel=r["channel"], tier=r["tier"], filed_at=_dt(r["filed_at"]),
                subject=r["subject"], body=r["body"],
                attachments=json.loads(r["attachments"]), reference=r["reference"],
                dry_run=bool(r["dry_run"]), response_raw=r["response_raw"],
            )
            for r in rows
        ]

    # --------------------------------------------------------------- agencies
    async def upsert_agency(self, agency: Agency) -> None:
        await self._write(
            """INSERT INTO agencies (id, name, level, state, channel, endpoint, email,
                   ref_format, ref_label, sla_overrides, jurisdiction_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, level=excluded.level, state=excluded.state,
                   channel=excluded.channel, endpoint=excluded.endpoint,
                   email=excluded.email, ref_format=excluded.ref_format,
                   ref_label=excluded.ref_label, sla_overrides=excluded.sla_overrides,
                   jurisdiction_note=excluded.jurisdiction_note""",
            (
                agency.id, agency.name, agency.level.value, agency.state,
                agency.channel.value, agency.endpoint, agency.email,
                agency.ref_format, agency.ref_label,
                json.dumps(agency.sla_overrides), agency.jurisdiction_note,
            ),
        )

    def _agency(self, row: sqlite3.Row) -> Agency:
        return Agency(
            id=row["id"], name=row["name"], level=row["level"], state=row["state"],
            channel=row["channel"], endpoint=row["endpoint"], email=row["email"],
            ref_format=row["ref_format"], ref_label=row["ref_label"],
            sla_overrides=json.loads(row["sla_overrides"]),
            jurisdiction_note=row["jurisdiction_note"],
        )

    async def get_agency(self, agency_id: str) -> Agency | None:
        row = await self._read_one("SELECT * FROM agencies WHERE id = ?", (agency_id,))
        return self._agency(row) if row else None

    async def list_agencies(self) -> list[Agency]:
        return [self._agency(r) for r in await self._read("SELECT * FROM agencies ORDER BY id")]

    # ------------------------------------------------------------------ stats
    async def stats(self, now: datetime) -> dict[str, float | int]:
        """The four numbers on the dashboard's stat band.

        'Missed by the official feed' is the headline: of everything we
        confirmed, how much did the state's own feed not already have? That
        ratio is the entire argument for this system existing.
        """
        # The week is measured back from the most recent filing rather than from
        # wall-clock now. For a system running continuously these are the same
        # thing; for a soak run or a demo being reviewed days later, anchoring to
        # the clock would report zero activity for a week that was actually busy.
        latest = await self._read_one("SELECT MAX(filed_at) AS latest FROM filings")
        anchor = _dt(latest["latest"]) if latest and latest["latest"] else now
        week_ago = (min(anchor, now) - timedelta(days=7)).isoformat()

        filed_week = await self._read_one(
            "SELECT COUNT(*) AS n FROM filings WHERE filed_at >= ?", (week_ago,)
        )
        confirmed = await self._read_one(
            """SELECT
                   SUM(CASE WHEN gate_decision = 'file' THEN 1 ELSE 0 END) AS filed,
                   SUM(CASE WHEN gate_decision = 'suppress' THEN 1 ELSE 0 END) AS suppressed
               FROM cases
               WHERE gate_decision IN ('file','suppress') AND synthetic = 0"""
        )
        n_filed = (confirmed["filed"] or 0) if confirmed else 0
        n_suppressed = (confirmed["suppressed"] or 0) if confirmed else 0
        total_confirmed = n_filed + n_suppressed

        # Median time from a case opening to its first filing.
        latency_rows = await self._read(
            """SELECT (julianday(f.filed_at) - julianday(c.opened_at)) * 86400 AS secs
               FROM filings f JOIN cases c ON c.id = f.case_id
               WHERE f.tier = 1 AND c.synthetic = 0 ORDER BY secs"""
        )
        latencies = [r["secs"] for r in latency_rows if r["secs"] is not None]
        median = latencies[len(latencies) // 2] if latencies else 0

        # Every number here is a public claim about what the system has done.
        # A drill is not something the system did to a road, so none of it counts.
        counts = await self._read(
            "SELECT kind, COUNT(*) AS n FROM cases WHERE synthetic = 0 GROUP BY kind"
        )
        by_kind = {r["kind"]: r["n"] for r in counts}
        total_cases = sum(by_kind.values())

        return {
            "filed_this_week": filed_week["n"] if filed_week else 0,
            "missed_by_feed_pct": round(100 * n_filed / total_confirmed) if total_confirmed else 0,
            "median_detect_to_file_seconds": int(median),
            "total_cases": total_cases,
            "open_cases": by_kind.get("watching", 0)
            + by_kind.get("filed", 0)
            + by_kind.get("escalated", 0),
            **{f"count_{k}": v for k, v in by_kind.items()},
        }
