"""Turning domain objects into things a template can render.

All the presentation that deliberately does *not* live in the domain -- colours,
percentages, formatted timestamps, CSS positions -- is computed here. That way
changing how a case looks never means touching a model or migrating a table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from road_cleaner.domain.enums import (
    CASE_KIND_LABELS,
    CHANNEL_LABELS,
    STAGE_LABELS,
    STATE_LABELS,
    CaseKind,
)
from road_cleaner.domain.models import Case, CaseWithDetail, FrameRef
from road_cleaner.domain.sla import elapsed_fraction, format_remaining, rationale_for

# Matches the TONE map in the design comps.
TONE_COLORS: dict[str, str] = {
    "filed": "#2F7A4F",
    "escalated": "#B4451F",
    "watching": "#8A6A10",
    "cleared": "#6B6B65",
    "suppressed": "#9A9A92",
}

# The SLA bar shifts colour as a case runs out of time.
SLA_COLORS = {
    "ok": "#0F5340",
    "close": "#F2C230",
    "over": "#E2622B",
    "closed": "#4A7A5E",
    "none": "#74776E",
}

FILTERS: list[dict[str, str]] = [
    {"key": "all", "label": "All"},
    {"key": "filed", "label": "Filed"},
    {"key": "watching", "label": "Watching"},
    {"key": "escalated", "label": "Past due"},
    {"key": "cleared", "label": "Cleared"},
]

STATES: list[dict[str, str]] = [
    {"key": "all", "label": "All states"},
    {"key": "GA", "label": "Georgia"},
    {"key": "FL", "label": "Florida"},
    {"key": "NC", "label": "N. Carolina"},
]


def tone(case: Case) -> str:
    return TONE_COLORS.get(case.kind.value, "#6B6B65")


def when(moment: datetime | None) -> str:
    """Short stamp used in the log, e.g. 'Mon 14:02'."""
    return moment.strftime("%a %H:%M") if moment else "—"


def frame_url(ref: FrameRef | None) -> str | None:
    if ref is None or not ref.blob_key:
        return None
    return f"/frames/{ref.blob_key}"


def box_style(case: Case) -> str | None:
    """Position the detection overlay from the model's own fractions."""
    if case.box is None:
        return None
    b = case.box
    return (
        f"left:{b.x * 100:.1f}%;top:{b.y * 100:.1f}%;"
        f"width:{b.width * 100:.1f}%;height:{b.height * 100:.1f}%"
    )


def evidence_frame(case: Case) -> FrameRef | None:
    """The frame that best represents the case: cleared shot, else key evidence."""
    clear = next((f for f in case.frame_refs if f.clear and f.blob_key), None)
    if case.kind is CaseKind.CLEARED and clear:
        return clear
    marked = next((f for f in case.frame_refs if f.mark and f.blob_key), None)
    return marked or next((f for f in case.frame_refs if f.blob_key), None)


def case_row(case: Case) -> dict[str, Any]:
    """One row in the road log."""
    frame = evidence_frame(case)
    return {
        "id": case.id,
        "kind": case.kind.value,
        "status": CASE_KIND_LABELS.get(case.kind.value, case.kind.value),
        "tone": tone(case),
        "hazard": case.hazard_title,
        "location": case.location,
        "when": when(case.opened_at),
        "sentence": case.sentence,
        "ref_label": case.ref_label or "NOTHING SENT",
        "ref": case.reference or f"held · {case.confidence:.2f}",
        "frame_url": frame_url(frame),
        "box_style": box_style(case),
        "box_label": case.box_label,
        "box_clear": bool(frame and frame.clear),
        "suppressed": case.kind is CaseKind.SUPPRESSED,
    }


def sla_view(case: Case, now: datetime) -> dict[str, Any]:
    """The time-remaining bar."""
    if case.closed_at is not None and case.kind is CaseKind.CLEARED:
        return {
            "note": case.ref_label or "closed",
            "color": SLA_COLORS["closed"],
            "pct": 100,
            "why": "Confirmed clear against the original evidence frame.",
        }
    if case.sla_deadline is None:
        return {
            "note": "no filing",
            "color": SLA_COLORS["none"],
            "pct": 0,
            "why": (
                "Nothing was sent, so no clock is running. This stays under "
                "observation until it clears or crosses the bar to report."
            ),
        }

    remaining = format_remaining(case.sla_deadline, now)
    fraction = elapsed_fraction(case.opened_at, case.sla_deadline, now)
    if remaining.endswith("overdue"):
        color = SLA_COLORS["over"]
    elif fraction > 0.75:
        color = SLA_COLORS["close"]
    else:
        color = SLA_COLORS["ok"]
    return {
        "note": remaining,
        "color": color,
        "pct": round(fraction * 100),
        "why": rationale_for(case.hazard_type),
    }


def case_detail(detail: CaseWithDetail, now: datetime) -> dict[str, Any]:
    """Everything the case page renders."""
    case = detail.case
    watching = case.kind in (CaseKind.WATCHING, CaseKind.FILED, CaseKind.ESCALATED)

    frames = [
        {
            "label": f.label,
            "time": f.captured_at.strftime("%H:%M:%S") if f.captured_at else "—",
            "url": frame_url(f),
            "mark": f.mark,
            "clear": f.clear,
        }
        for f in case.frame_refs
    ]

    return {
        "case": case,
        "state_code": case.state_code,
        "number": case.number,
        "status": CASE_KIND_LABELS.get(case.kind.value, case.kind.value),
        "tone": tone(case),
        "watching": watching,
        "camera_label": detail.camera.label if detail.camera else case.camera_id,
        "road": (
            f"{detail.camera.road} {detail.camera.direction or ''}".strip().upper()
            if detail.camera
            else ""
        ),
        "county": detail.camera.county if detail.camera else None,
        "live_frame": frame_url(evidence_frame(case)),
        "box_style": box_style(case),
        "box_label": case.box_label,
        "frames": frames,
        "trail": [
            {
                "time": t.at.strftime("%a %H:%M:%S"),
                "text": t.text,
                "tone": t.tone.value,
                "stage": STAGE_LABELS.get(t.stage.value, t.stage.value),
            }
            for t in detail.trail
        ],
        "agency": detail.agency.name if detail.agency else None,
        "channel": (
            CHANNEL_LABELS.get(case.channel.value, case.channel.value)
            if case.channel
            else "Nothing sent"
        ),
        "reference": case.reference or "—",
        "ref_label": case.ref_label or "NOTHING SENT",
        "sla": sla_view(case, now),
        "letter": detail.filings[-1].body if detail.filings else None,
        "letter_subject": detail.filings[-1].subject if detail.filings else None,
        "was_dry_run": all(f.dry_run for f in detail.filings) if detail.filings else True,
        "filing_count": len(detail.filings),
        "last_checked": when(case.last_checked_at),
        "model_json": case.raw_model_json,
        "model_name": (
            detail.detections[-1].model_name if detail.detections else "scripted"
        ),
        "explain": case.explain,
    }


def summary_line(counts: dict[str, int]) -> str:
    """The mono line under the heading, e.g. '11 cases · 4 filed · 2 cleared'."""
    total = sum(counts.values())
    if not total:
        return "no cases yet"
    parts = [f"{total} case{'s' if total != 1 else ''}"]
    for kind in ("filed", "cleared", "escalated", "watching"):
        if counts.get(kind):
            parts.append(f"{counts[kind]} {CASE_KIND_LABELS[kind].lower()}")
    if counts.get("suppressed"):
        parts.append(f"{counts['suppressed']} stood down")
    return " · ".join(parts)


def stat_band(stats: dict[str, Any]) -> list[dict[str, str]]:
    """The four headline numbers.

    'The official feed never saw' is the one that matters -- it is the entire
    argument for this system existing, and it comes straight from the gate's
    own suppression counts rather than being asserted.
    """
    median = int(stats.get("median_detect_to_file_seconds", 0))
    if median >= 3600:
        latency, unit = f"{median // 3600}", "h"
    elif median >= 60:
        latency, unit = f"{median // 60}", "m"
    else:
        latency, unit = f"{median}", "s"

    return [
        {
            "value": f"{stats.get('filed_this_week', 0)}",
            "unit": "",
            "label": "reports filed this week",
        },
        {
            "value": f"{stats.get('missed_by_feed_pct', 0)}",
            "unit": "%",
            "label": "the official feed never saw",
        },
        {"value": latency, "unit": unit, "label": "median spot → filed"},
        {
            "value": f"{stats.get('open_cases', 0)}",
            "unit": "",
            "label": "still being chased",
        },
    ]


def state_label(code: str) -> str:
    return STATE_LABELS.get(code, code)
