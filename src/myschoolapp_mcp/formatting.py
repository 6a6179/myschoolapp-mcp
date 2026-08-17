"""Pure formatting/parsing helpers for the MCP server.

Everything in here is side-effect free: no network, no env vars, no file
I/O. That keeps it unit-testable without a live session.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from typing import Any

# .NET int.MinValue — Blackbaud's "not set" sentinel.
SENTINEL = -2147483648


def mdy(d: str | None) -> str:
    """Convert an ISO date string to the M/D/YYYY format the API expects."""
    if not d:
        return ""
    try:
        parsed = date.fromisoformat(d.strip())
    except ValueError:
        raise ValueError(f"Invalid date {d!r}: expected YYYY-MM-DD.") from None
    return f"{parsed.month}/{parsed.day}/{parsed.year}"


def or_none(v: Any) -> Any:
    """Collapse empty strings to None while preserving 0 and False."""
    return None if v == "" else v


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

ASSIGNMENT_BUCKETS = (
    "Missing",
    "Overdue",
    "DueToday",
    "DueTomorrow",
    "DueThisWeek",
    "DueNextWeek",
    "DueAfterNextWeek",
    "PastThisWeek",
    "PastLastWeek",
    "PastBeforeLastWeek",
)

# PastBeforeLastWeek can contain hundreds of items, so it's opt-in.
DEFAULT_ASSIGNMENT_BUCKETS = tuple(
    b for b in ASSIGNMENT_BUCKETS if b != "PastBeforeLastWeek"
)

# Canonical output field -> candidate keys in the endpoint response.
# DataDirect endpoints return lowercase keys; the CamelCase variants are
# kept as fallbacks in case a deployment differs.
ASSIGNMENT_FIELD_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("assignment_id", ("assignment_id", "AssignmentId")),
    ("assignment_index_id", ("assignment_index_id", "AssignmentIndexId")),
    ("section_id", ("section_id", "SectionId")),
    ("class", ("groupname", "group_name", "GroupName")),
    ("title", ("short_description", "ShortDescription")),
    ("type", ("assignment_type", "AssignmentType")),
    ("assigned", ("date_assigned", "DateAssigned")),
    ("due", ("date_due", "DateDue")),
    ("max_points", ("maxpoints", "max_points", "MaxPoints")),
    ("status", ("assignment_status", "AssignmentStatus", "StudentStatus")),
    ("missing", ("missing_ind", "MissingInd")),
    ("late", ("late_ind", "LateInd")),
    ("incomplete", ("incomplete_ind", "IncompleteInd")),
    ("major", ("major", "Major")),
    ("extra_credit", ("extra_credit", "ExtraCredit")),
    ("marking_period", ("marking_period_description", "MarkingPeriodDescription")),
    ("has_grade", ("has_grade", "HasGrade")),
    ("drop_box", ("dropbox_ind", "drop_box_ind", "DropBoxInd")),
)

_ASSIGNMENT_CANDIDATES: dict[str, tuple[str, ...]] = dict(ASSIGNMENT_FIELD_MAP)


def assignment_field(item: dict[str, Any], canon: str) -> Any:
    for k in _ASSIGNMENT_CANDIDATES.get(canon, ()):
        if k in item:
            return item[k]
    return None


def compact_assignment(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for canon, candidates in ASSIGNMENT_FIELD_MAP:
        for k in candidates:
            if k in item:
                out[canon] = item[k]
                break
    if "due" not in out and "title" not in out:
        # Unrecognized schema — pass the item through rather than silently
        # dropping every field.
        return dict(item)
    return out


def parse_assignment_date(raw: Any) -> date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def due_bucket(due: date, today: date) -> str:
    week_start = today - timedelta(days=today.weekday())  # Monday
    if due == today:
        return "DueToday"
    if due == today + timedelta(days=1):
        return "DueTomorrow"
    if due > today:
        if due <= week_start + timedelta(days=6):
            return "DueThisWeek"
        if due <= week_start + timedelta(days=13):
            return "DueNextWeek"
        return "DueAfterNextWeek"
    if due >= week_start:
        return "PastThisWeek"
    if due >= week_start - timedelta(days=7):
        return "PastLastWeek"
    return "PastBeforeLastWeek"


# AssignmentStatusType enum, decoded from the lms-assignment SPA bundle.
# The sentinel is treated as ToDo by the SPA too.
STATUS_TYPE_LABELS: dict[int, str] = {
    SENTINEL: "To do",
    -1: "To do",
    0: "In progress",
    1: "Completed",
    2: "Overdue",
    3: "Retake",
    4: "Graded",
    6: "Paused",
}


def status_label(t: Any) -> str | None:
    if t is None:
        return None
    try:
        return STATUS_TYPE_LABELS.get(int(t))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------

_HTML_BLOCK_TAGS = re.compile(
    r"</\s*(p|div|li|h[1-6]|tr|blockquote|pre)\s*>", re.IGNORECASE
)
_HTML_BREAK_TAGS = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_HTML_LIST_ITEM = re.compile(r"<\s*li[^>]*>", re.IGNORECASE)
_HTML_LINK = re.compile(
    r'<\s*a\b[^>]*?href\s*=\s*"([^"]+)"[^>]*>(.*?)</\s*a\s*>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_WS_RUN = re.compile(r"[ \t]+")
_NL_RUN = re.compile(r"\n{3,}")


def strip_html(s: Any) -> str | None:
    """Turn the SPA's HTML descriptions into readable plain text."""
    if not isinstance(s, str) or not s.strip():
        return None
    out = s
    # Anchors → "label (url)" so the model still sees the destination.
    out = _HTML_LINK.sub(lambda m: f"{m.group(2).strip()} ({m.group(1).strip()})", out)
    out = _HTML_LIST_ITEM.sub("\n- ", out)
    out = _HTML_BREAK_TAGS.sub("\n", out)
    out = _HTML_BLOCK_TAGS.sub("\n\n", out)
    out = _HTML_TAG.sub("", out)
    out = html.unescape(out)
    out = _WS_RUN.sub(" ", out)
    out = "\n".join(line.rstrip() for line in out.splitlines())
    out = _NL_RUN.sub("\n\n", out).strip()
    return out or None


# ---------------------------------------------------------------------------
# Grades
# ---------------------------------------------------------------------------


def fmt_pct(n: Any) -> str | None:
    """Format a number like 85.39 as '85.39%'. Returns None for empty/zero."""
    f = to_float(n)
    return None if f is None else f"{f:.2f}%"


def to_float(n: Any) -> float | None:
    """Parse a grade value. The API uses 0 for 'no grade yet', so exact 0
    maps to None rather than 0%."""
    if n is None or n == "":
        return None
    try:
        f = float(n)
    except (TypeError, ValueError):
        return None
    return f if f != 0 else None


def format_range(low: Any, high: Any) -> str | None:
    """Render a rubric level point range like '3.1-4' or just '4'."""
    if low is None and high is None:
        return None
    if low is None:
        return str(high)
    if high is None or low == high:
        return str(low)
    return f"{low}-{high}"


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def clean_schedule_item(item: dict[str, Any]) -> dict[str, Any]:
    """Compact one MyDayCalendarStudentList row (~50 raw fields, mostly
    zeros, sentinels, and '1/1/1900' dates) down to what matters."""
    out: dict[str, Any] = {
        "class": item.get("CourseTitle"),
        "block": item.get("Block"),
        "start": item.get("MyDayStartTime"),
        "end": item.get("MyDayEndTime"),
        "room": item.get("RoomNumber"),
        "building": item.get("BuildingName"),
        "teacher": item.get("Contact"),
        "teacher_email": item.get("ContactEmail"),
        "section_id": item.get("SectionId"),
        "attendance": item.get("AttendanceDisplay"),
    }
    # Athletics-only fields.
    if item.get("ScheduleItemType"):
        out["type"] = item["ScheduleItemType"]
    if item.get("Opponent"):
        out["opponent"] = item["Opponent"]
        out["home_away"] = item.get("AthHomeAway")
    if item.get("CanceledInd"):
        out["canceled"] = True
    if item.get("RescheduledInd"):
        out["rescheduled"] = True
    if item.get("RescheduledNote"):
        out["rescheduled_note"] = item["RescheduledNote"]
    return {k: v for k, v in out.items() if v not in (None, "", SENTINEL)}


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

# Raw ParentStudentUserClassesGet key -> canonical snake_case name. The raw
# response mixes lowercase-mashed and PascalCase keys; normalize them.
_CLASS_KEY_MAP: dict[str, str] = {
    "sectionid": "section_id",
    "leadsectionid": "lead_section_id",
    "sectionidentifier": "class",
    "room": "room",
    "currentterm": "current_term",
    "DurationId": "duration_id",
    "markingperiodid": "marking_period_id",
    "groupownername": "teacher",
    "groupowneremail": "teacher_email",
    "OwnerId": "teacher_id",
    "cumgrade": "current_grade",
    "CumulativeDisplay": "current_grade_display",
    "OverdueCount": "overdue_count",
    "UpcomingCount": "upcoming_count",
    "assignmentactivetoday": "assignments_active_today",
    "assignmentduetoday": "assignments_due_today",
    "assignmentassignedtoday": "assignments_assigned_today",
    "canviewassignments": "can_view_assignments",
    "AttendanceTaken": "attendance_taken",
}


def compact_class(c: dict[str, Any]) -> dict[str, Any]:
    out = {canon: c[raw] for raw, canon in _CLASS_KEY_MAP.items() if raw in c}
    # cumgrade uses 0 for "no grade yet" — honor the same convention
    # gradebook() does instead of presenting it as a real 0%.
    if "current_grade" in out:
        out["current_grade"] = to_float(out["current_grade"])
    if "current_grade_display" in out:
        out["current_grade_display"] = or_none(out["current_grade_display"])
    return out


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------


def compact_directory_entry(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "user_id": item.get("UserID"),
        "name": (item.get("UserNameFormatted") or "").strip() or None,
        "email": item.get("Email") or None,
        "phone": (item.get("OfficePhone") or "").strip() or None,
        "job_title": item.get("JobTitle") or None,
        "department": item.get("DepartmentDisplay") or None,
        "grad_year": item.get("GradYear") or None,
        "grade": item.get("Grade") or None,
        "is_student": bool(item.get("IsStudentInd")),
    }
    return {k: v for k, v in out.items() if v is not None}
