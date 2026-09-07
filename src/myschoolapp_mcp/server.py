"""MCP server exposing myschoolapp.com via the MCP Python SDK.

Endpoints were mapped by capturing live network traffic from a logged-in
student session on Tabor Academy's myschoolapp deployment. They follow the
standard Blackbaud onMSA SPA conventions and should work on any school's
instance, though some IDs (categoryId, durationId, directoryId) are
school-specific.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .client import MyschoolappClient, load_env_file
from .formatting import (
    ASSIGNMENT_BUCKETS,
    DEFAULT_ASSIGNMENT_BUCKETS,
    STATUS_TYPE_LABELS,
    assignment_field,
    clean_schedule_item,
    compact_assignment,
    compact_class,
    compact_directory_entry,
    due_bucket,
    fmt_pct,
    format_range,
    mdy,
    or_none,
    parse_assignment_date,
    status_label,
    strip_html,
    to_float,
)

_ENV_PATH = load_env_file()

mcp = MCPServer("myschoolapp", version=__version__)
_client: MyschoolappClient | None = None
_client_lock = threading.Lock()


def _get_client() -> MyschoolappClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = MyschoolappClient()
        return _client


def _drop_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            with contextlib.suppress(Exception):
                _client.close()
            _client = None


def _student_id() -> str:
    sid = os.environ.get("MSA_STUDENT_ID")
    if not sid:
        raise RuntimeError(
            "Set MSA_STUDENT_ID to your numeric persona user id. Find it in "
            "any /api/user/profiletabs?showuserid=... request in DevTools."
        )
    return sid


def _persona_id() -> str:
    return os.environ.get("MSA_PERSONA_ID", "2")  # 2 = student


def _school_year() -> str:
    sy = os.environ.get("MSA_SCHOOL_YEAR")
    if sy:
        return sy
    today = date.today()
    if today.month >= 7:
        return f"{today.year} - {today.year + 1}"
    return f"{today.year - 1} - {today.year}"


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Verify session cookie and return current user's profile tabs.

    If this errors with an HTML body or 401/403, the cookie is expired or
    wrong — run `cookie_refresh`.
    """
    return _get_client().request(
        "GET",
        "/api/user/profiletabs",
        params={"showuserid": _student_id(), "personaId": _persona_id()},
    )


@mcp.tool()
def config() -> dict[str, Any]:
    """Show resolved config (subdomain, student id, persona, school year)."""
    client = _get_client()
    return {
        "subdomain": client.subdomain or "",
        "student_id": os.environ.get("MSA_STUDENT_ID")
        or "(unset — set MSA_STUDENT_ID; most tools will fail without it)",
        "persona_id": _persona_id(),
        "school_year": _school_year(),
        "env_file": str(_ENV_PATH) if _ENV_PATH else None,
    }


@mcp.tool()
async def cookie_refresh() -> dict[str, Any]:
    """Re-run the Playwright login flow to refresh the session cookie.

    Drives a fresh Microsoft OAuth login using SCHOOL_EMAIL / SCHOOL_PASS
    from the environment, writes the new cookie to MSA_COOKIES_FILE (or the
    default ~/.myschoolapp-mcp/cookie.txt), then drops the cached HTTP
    client so subsequent tool calls use the new cookie.

    Use this when other tools start returning HTML or 401/403 — i.e. when
    the cookie has expired.

    Takes roughly 10-30 seconds. Requires playwright + chromium installed
    on the host. Does not work on 2FA / MFA accounts. On a server with a
    new IP, Microsoft may demand additional verification and the flow will
    fail; in that case, refresh on a trusted machine and copy the cookie
    file over.
    """
    from .auth import refresh_cookie

    # refresh_cookie() uses Playwright's sync API, which cannot run inside
    # the asyncio event loop the MCP server runs us in. Offload to a worker
    # thread.
    path = await asyncio.to_thread(refresh_cookie)
    _drop_client()
    return {"ok": True, "cookie_path": str(path)}


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


@mcp.tool()
def assignments(
    display_by_due_date: bool = True,
    buckets: str = "",
    full: bool = False,
) -> dict[str, Any]:
    """Get assignments grouped by due-date bucket (or by class).

    Buckets are computed locally from the DataDirect date-range endpoint —
    the same one the site's working legacy assignment view uses. (The
    /api/assignment2/StudentAssignmentCenterGet endpoint this tool used to
    call returns 403 for student sessions, even in a real browser.)

    Available buckets: Missing, Overdue, DueToday, DueTomorrow, DueThisWeek,
    DueNextWeek, DueAfterNextWeek, PastThisWeek, PastLastWeek,
    PastBeforeLastWeek. Weeks run Monday-Sunday. PastBeforeLastWeek is
    excluded by default because it can contain hundreds of items; requesting
    it widens the fetch window to ~6 months back. Missing/Overdue come from
    the server's missing/overdue filter; items flagged there are removed
    from the date buckets so nothing is listed twice.

    Each compact item includes assignment_id, assignment_index_id (pass to
    `assignment_detail`), section_id, class, title, type, assigned, due,
    max_points, status (decode with `assignment_status_labels()`), missing,
    late, incomplete, major, extra_credit, marking_period, has_grade, and
    drop_box.

    Args:
        display_by_due_date: True = group by due-date bucket (default).
            False = group by class name instead (no Missing/Overdue merge).
        buckets: Comma-separated bucket names to include. Empty = default
            set (everything except PastBeforeLastWeek). Use "all" to include
            every bucket. Unknown names raise ValueError in either mode.
            When display_by_due_date is False this only controls how far
            back the fetch window reaches (PastBeforeLastWeek widens it).
        full: True = return the raw, untrimmed endpoint responses.
    """
    if not buckets:
        wanted = set(DEFAULT_ASSIGNMENT_BUCKETS)
    elif buckets.strip().lower() == "all":
        wanted = set(ASSIGNMENT_BUCKETS)
    else:
        wanted = {b.strip() for b in buckets.split(",") if b.strip()}
        unknown = wanted - set(ASSIGNMENT_BUCKETS)
        if unknown:
            raise ValueError(
                f"Unknown bucket(s) {sorted(unknown)}. "
                f"Valid: {', '.join(ASSIGNMENT_BUCKETS)} (or 'all')."
            )

    client = _get_client()
    today = date.today()
    days_back = 180 if "PastBeforeLastWeek" in wanted else 21
    common = {
        "format": "json",
        "persona": _persona_id(),
        "statusList": "",
        "sectionList": "",
    }
    resp = client.request(
        "GET",
        "/api/DataDirect/AssignmentCenterAssignments/",
        params={
            **common,
            "filter": 0,
            "dateStart": mdy((today - timedelta(days=days_back)).isoformat()),
            "dateEnd": mdy((today + timedelta(days=60)).isoformat()),
        },
    )

    # Missing/overdue can predate the main window, so look back far. Only
    # needed for the bucketed view (and raw dumps). A transport failure here
    # must not throw away the already-successful range fetch — degrade to an
    # error dict and let the soft-failure path below add a note.
    flagged: dict[str, Any] | None = None
    if full or display_by_due_date:
        try:
            flagged = client.request(
                "GET",
                "/api/DataDirect/AssignmentCenterAssignments/",
                params={
                    **common,
                    "filter": 3,
                    "dateStart": mdy((today - timedelta(days=180)).isoformat()),
                    "dateEnd": mdy(today.isoformat()),
                },
            )
        except RuntimeError as e:
            flagged = {"error": True, "status": None, "message": str(e)}

    if full:
        return {"range": resp, "missing_overdue": flagged}
    # Never dress an error up as an empty assignment list.
    if resp.get("error") or not isinstance(resp.get("body"), list):
        return resp

    items = resp["body"]
    out_buckets: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    unbucketed: list[dict[str, Any]] = []
    note: str | None = None

    scan_items = items  # what sections/major_assignments are built from
    if display_by_due_date:
        assert flagged is not None
        flagged_items: list[dict[str, Any]] = []
        if flagged.get("error") or not isinstance(flagged.get("body"), list):
            reason = flagged.get("message") or f"status {flagged.get('status')}"
            note = (
                f"missing/overdue fetch failed ({reason}); "
                "Missing/Overdue buckets may be incomplete"
            )
        else:
            flagged_items = flagged["body"]
        flagged_ids = {
            assignment_field(i, "assignment_index_id") for i in flagged_items
        }
        flagged_ids.discard(None)
        # Flagged items can predate the range window, so include them when
        # scanning for sections and major assignments too (dedup by index id
        # mirrors the bucketing logic below).
        scan_items = [
            i
            for i in items
            if assignment_field(i, "assignment_index_id") not in flagged_ids
        ] + flagged_items

        by_bucket: dict[str, list[dict[str, Any]]] = {
            name: [] for name in ASSIGNMENT_BUCKETS
        }
        for item in items:
            idx = assignment_field(item, "assignment_index_id")
            if idx is not None and idx in flagged_ids:
                continue  # listed under Missing/Overdue instead
            due = parse_assignment_date(assignment_field(item, "due"))
            if due is None:
                unbucketed.append(compact_assignment(item))
                continue
            by_bucket[due_bucket(due, today)].append(compact_assignment(item))
        for item in flagged_items:
            name = "Missing" if assignment_field(item, "missing") else "Overdue"
            by_bucket[name].append(compact_assignment(item))
        for name in ASSIGNMENT_BUCKETS:
            counts[name] = len(by_bucket[name])
            if name in wanted:
                out_buckets[name] = by_bucket[name]
    else:
        for item in items:
            key = str(assignment_field(item, "class") or "Unknown class")
            out_buckets.setdefault(key, []).append(compact_assignment(item))
        counts = {k: len(v) for k, v in out_buckets.items()}

    seen_sections: dict[Any, dict[str, Any]] = {}
    major: list[dict[str, Any]] = []
    for item in scan_items:
        sec_id = assignment_field(item, "section_id")
        if sec_id is not None and sec_id not in seen_sections:
            seen_sections[sec_id] = {
                "section_id": sec_id,
                "class": assignment_field(item, "class"),
            }
        if assignment_field(item, "major"):
            major.append(compact_assignment(item))

    out = {
        "status": resp.get("status"),
        "url": resp.get("url"),
        "counts": counts,
        "buckets_included": sorted(out_buckets.keys()),
        "buckets": out_buckets,
        "sections": list(seen_sections.values()),
        "major_assignments": major,
    }
    if unbucketed:
        out["unbucketed"] = unbucketed
    if note:
        out["note"] = note
    return out


@mcp.tool()
def assignment_status_labels() -> dict[str, str]:
    """Map `status` codes returned by `assignments` to readable labels.

    Labels come from the AssignmentStatusType enum in the site's
    lms-assignment SPA bundle. The DataDirect list endpoint appears to use
    the same codes, but that mapping hasn't been verified on every
    deployment — treat unfamiliar codes with mild suspicion.
    """
    return {str(k): v for k, v in STATUS_TYPE_LABELS.items()}


@mcp.tool()
def assignments_in_range(
    date_start: str | None = None,
    date_end: str | None = None,
    filter_type: int = 1,
    status_list: str = "",
    section_list: str = "",
) -> dict[str, Any]:
    """List assignments in a specific date range (legacy DataDirect endpoint).

    Use `assignments()` for the standard bucketed view; use this when you
    need a custom date window. Note: data from previous school years is not
    retained by the endpoint — old ranges return [].

    Args:
        date_start: YYYY-MM-DD. Defaults to today.
        date_end: YYYY-MM-DD. Defaults to today + 30 days.
        filter_type: 0=all, 1=upcoming, 2=past, 3=missing/overdue.
        status_list: Comma-separated assignment status filter.
        section_list: Comma-separated section ids.
    """
    if not date_start:
        date_start = date.today().isoformat()
    if not date_end:
        date_end = (date.today() + timedelta(days=30)).isoformat()
    return _get_client().request(
        "GET",
        "/api/DataDirect/AssignmentCenterAssignments/",
        params={
            "format": "json",
            "filter": filter_type,
            "dateStart": mdy(date_start),
            "dateEnd": mdy(date_end),
            "persona": _persona_id(),
            "statusList": status_list,
            "sectionList": section_list,
        },
    )


@mcp.tool()
def missing_assignments() -> dict[str, Any]:
    """Quick check for missing/overdue assignments."""
    return _get_client().request(
        "GET",
        "/api/datadirect/StudentMissingAssignmentCheck",
        params={"studentId": _student_id()},
    )


@mcp.tool()
def assignment_options() -> dict[str, Any]:
    """Get available assignment view options (statuses, filters)."""
    return _get_client().request("GET", "/api/Assignment/ViewAssignmentOptions")


def _absolute_url(client: MyschoolappClient, url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        return f"{client.base_url}{url}"
    return f"{client.base_url}/{url}"


def _submission_method(body: dict[str, Any]) -> str | None:
    if body.get("DropboxInd"):
        return "dropbox"
    if body.get("AssessmentInd"):
        return "assessment"
    if body.get("DiscussionInd"):
        return "discussion"
    if body.get("OnPaperSubmission"):
        return "on_paper"
    return None


def _clean_resource(client: MyschoolappClient, item: dict[str, Any]) -> dict[str, Any]:
    """Compact a DownloadItems / LinkItems entry to {name, url}."""
    return {
        "name": item.get("FriendlyFileName")
        or item.get("ShortDescription")
        or item.get("Url"),
        "url": _absolute_url(client, item.get("DownloadUrl") or item.get("Url")),
    }


def _clean_submission(client: MyschoolappClient, s: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": s.get("FileName"),
        "submitted_at": s.get("LastSubmitDate"),
        "url": _absolute_url(client, s.get("DownloadUrl")),
    }
    if s.get("GoogleExternalUrl"):
        out["google_url"] = s["GoogleExternalUrl"]
    if s.get("Detail"):
        out["detail"] = s["Detail"]
    return out


@mcp.tool()
def assignment_detail(
    assignment_index_id: int | str,
    include_rubric: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """View a single assignment with description, downloads, and submission.

    The `assignment_index_id` is the number at the end of the assignment URL,
    e.g. `/lms-assignment/assignment/assignment-student-view/41609904` →
    `41609904`. This is the per-student index id, NOT the global
    assignment_id. Use the assignment_index_id field from the
    `assignments()` list, or grab it from the browser URL.

    Slim output (default) returns a flat snake_case structure:
    - title, class, type, assigned, due, max_points, status, past_due
    - description (HTML decoded to plain text)
    - submission: {method, submitted, submitted_at, can_resubmit, files[]}
    - resources: {downloads[], links[]} with absolute URLs
    - grade / comment / late / missing / exempt / incomplete

    Set `include_rubric=True` to also fetch the rubric definition and any
    per-criterion scores the teacher has assigned (results may be empty if
    the assignment hasn't been graded yet).

    Note: this tool is read-only. It does not upload submission files —
    use the website for that.

    Args:
        assignment_index_id: The index id from the assignment URL.
        include_rubric: Also fetch the rubric definition + student results.
        full: Return raw, untrimmed responses (large; for debugging).
    """
    client = _get_client()
    aid = str(assignment_index_id)
    sid = _student_id()
    detail = client.request(
        "GET",
        "/api/assignment2/UserAssignmentDetailsGetAllStudentData",
        params={
            "assignmentIndexId": aid,
            "studentUserId": sid,
            "personaId": _persona_id(),
        },
    )

    rubric_resp: dict[str, Any] | None = None
    rubric_results_resp: dict[str, Any] | None = None
    if include_rubric:
        body = detail.get("body") if isinstance(detail, dict) else None
        rubric_id = body.get("RubricId") if isinstance(body, dict) else None
        if rubric_id:
            rubric_resp = client.request(
                "GET",
                "/api/Rubric/AssignmentRubric/",
                params={"id": str(rubric_id)},
            )
            rubric_results_resp = client.request(
                "GET",
                "/api/Rubric/RubricResultsGet/",
                params={"assignmentIndexId": aid, "studentId": sid},
            )

    if full:
        out: dict[str, Any] = {"detail": detail}
        if rubric_resp is not None:
            out["rubric"] = rubric_resp
            out["rubric_results"] = rubric_results_resp
        return out

    body = detail.get("body") if isinstance(detail, dict) else None
    if not isinstance(body, dict):
        return {"detail": detail}

    grade = body.get("AssignmentGrade") or {}

    cleaned: dict[str, Any] = {
        "id": body.get("AssignmentIndexId"),
        "assignment_id": body.get("AssignmentId"),
        "section_id": body.get("SectionId"),
        "url": (
            f"{client.base_url}/lms-assignment/assignment/assignment-student-view/{aid}"
        ),
        "title": body.get("ShortDescription"),
        "class": body.get("GroupName"),
        "type": body.get("AssignmentType"),
        "assigned": body.get("AssignmentDate"),
        "due": body.get("DueDate") or grade.get("DateDue"),
        "max_points": body.get("MaxPoints"),
        "extra_credit": bool(body.get("ExtraCredit")),
        "past_due": bool(body.get("PastDue")),
        "course_ended": bool(body.get("CourseEnded")),
        "status": status_label(grade.get("AssignmentStatusType")),
        # or_none, not `or`: a real 0 score must survive.
        "grade": or_none(grade.get("Grade")),
        "comment": or_none(grade.get("GradedComment")),
        "graded": bool(grade.get("HasGrade")),
        "late": bool(grade.get("Late")),
        "missing": bool(grade.get("Missing")),
        "exempt": bool(grade.get("Exempt")),
        "incomplete": bool(grade.get("Incomplete")),
        "collected": bool(grade.get("Collected")),
        "description": strip_html(body.get("LongDescription")),
    }

    method = _submission_method(body)
    submission_files = [
        _clean_submission(client, s) for s in (body.get("SubmissionResults") or [])
    ]
    cleaned["submission"] = {
        "method": method,
        "max_files": body.get("DropboxNumFiles") if method == "dropbox" else None,
        "submitted": bool(submission_files),
        "submitted_at": grade.get("LastSubmitDate"),
        "can_resubmit": bool(body.get("CanResubmit")),
        "files": submission_files,
    }

    cleaned["resources"] = {
        "downloads": [
            _clean_resource(client, d) for d in (body.get("DownloadItems") or [])
        ],
        "links": [
            _clean_resource(client, link) for link in (body.get("LinkItems") or [])
        ],
    }

    out = {"assignment": cleaned}

    if rubric_resp is not None:
        rb_body = rubric_resp.get("body") if isinstance(rubric_resp, dict) else None
        results_body = (
            rubric_results_resp.get("body")
            if isinstance(rubric_results_resp, dict)
            else None
        )
        rubric_out: dict[str, Any] = {}
        if isinstance(rb_body, dict):
            rubric_out = {
                "id": rb_body.get("RubricId") or body.get("RubricId"),
                "name": rb_body.get("Name"),
                "description": strip_html(rb_body.get("Description")),
                "criteria": [
                    {
                        "name": skl.get("Name"),
                        "description": strip_html(skl.get("Description")),
                        "levels": [
                            {
                                "name": lv.get("Name"),
                                "description": strip_html(lv.get("Description")),
                                "points": format_range(
                                    lv.get("Points"), lv.get("PointsTo")
                                ),
                            }
                            for lv in sorted(
                                (skl.get("Levels") or []),
                                key=lambda x: x.get("SortOrder") or 0,
                            )
                        ],
                    }
                    for skl in sorted(
                        (rb_body.get("Skills") or []),
                        key=lambda x: x.get("SortOrder") or 0,
                    )
                ],
            }
        else:
            rubric_out = {"raw": rubric_resp}

        if isinstance(results_body, list):
            rubric_out["results"] = [
                {
                    "criterion": r.get("SkillName") or r.get("Name"),
                    "level": r.get("LevelName"),
                    "points": r.get("Points"),
                    "comment": strip_html(r.get("Comment")) or None,
                }
                for r in results_body
            ]
        elif isinstance(results_body, dict):
            rubric_out["results"] = results_body
        else:
            rubric_out["results"] = []

        out["rubric"] = rubric_out

    return out


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@mcp.tool()
def schedule(on_date: str | None = None, full: bool = False) -> dict[str, Any]:
    """Get the day's schedule: classes, athletics, and other blocks.

    Compact items (default): class, block, start, end, room, building,
    teacher, teacher_email, section_id, attendance ("Attended" / "--" /
    "N/A"), plus type / opponent / home_away for athletics and
    canceled / rescheduled flags when set.

    Args:
        on_date: YYYY-MM-DD. Empty/None = today.
        full: True = raw endpoint response (~50 mostly-empty fields per item).
    """
    resp = _get_client().request(
        "GET",
        "/api/schedule/MyDayCalendarStudentList/",
        params={"scheduleDate": mdy(on_date), "personaId": _persona_id()},
    )
    if full or resp.get("error") or not isinstance(resp.get("body"), list):
        return resp
    resp["body"] = [clean_schedule_item(i) for i in resp["body"]]
    return resp


@mcp.tool()
def daily_announcement(on_date: str | None = None) -> dict[str, Any]:
    """Get the daily announcement.

    Args:
        on_date: YYYY-MM-DD. Empty/None = today.
    """
    return _get_client().request(
        "GET",
        "/api/schedule/ScheduleCurrentDayAnnouncmentParentStudent/",
        params={
            "mydayDate": mdy(on_date),
            "viewerId": _student_id(),
            "viewerPersonaId": _persona_id(),
        },
    )


# ---------------------------------------------------------------------------
# Academics
# ---------------------------------------------------------------------------


@mcp.tool()
def student_terms() -> dict[str, Any]:
    """List the student's terms (durations) for the school year.

    Each row has DurationId + DurationDescription (e.g. "Year Long",
    "Fall Season"), OfferingType (1 = academics, 2 = activities,
    3 = advisory, 4 = dorm, 9 = athletics, 11 = community), and CurrentInd
    (1 = term is currently active). DurationId values feed `classes`,
    `gradebook`, and `group_membership`; those tools auto-resolve the
    current term for their offering type when omitted. Community uses 0.
    """
    return _get_client().request(
        "GET",
        "/api/DataDirect/StudentGroupTermList/",
        params={
            "studentUserId": _student_id(),
            "schoolYearLabel": _school_year(),
            "personaId": _persona_id(),
        },
    )


def _resolve_duration_id(offering_type: int = 1) -> int:
    """Pick the current term for an offering type (academics by default)."""
    resp = student_terms()
    body = resp.get("body")
    if resp.get("error") or not isinstance(body, list):
        raise RuntimeError(
            f"Could not auto-resolve duration_id (status {resp.get('status')}). "
            "Pass duration_id explicitly — see student_terms()."
        )
    current = [
        t
        for t in body
        if isinstance(t, dict)
        and t.get("CurrentInd")
        and t.get("DurationId")
        and t.get("OfferingType") == offering_type
    ]
    if not current:
        raise RuntimeError(
            f"No currently-active term for OfferingType {offering_type} found. "
            "Pass duration_id explicitly — see student_terms()."
        )
    return int(current[0]["DurationId"])


def _fetch_classes(duration_id: int, marking_period_id: str = "") -> dict[str, Any]:
    return _get_client().request(
        "GET",
        "/api/datadirect/ParentStudentUserClassesGet",
        params={
            "userId": _student_id(),
            "schoolYearLabel": _school_year(),
            "memberLevel": 3,
            "persona": _persona_id(),
            "durationList": duration_id,
            "markingPeriodId": marking_period_id,
        },
    )


@mcp.tool()
def classes(
    duration_id: int = 0, marking_period_id: str = "", full: bool = False
) -> dict[str, Any]:
    """List classes for a term (duration).

    By default returns a compact snake_case view per class: section ids,
    class name, teacher (+email), room, current term, current grade, and
    assignment counts. Course descriptions, photos, and other heavy fields
    are stripped — set ``full=True`` to keep them.

    Args:
        duration_id: A DurationId from `student_terms()`. 0 / omitted =
            auto-resolve the current academic term.
        marking_period_id: Optional marking-period filter.
        full: True = return the raw, untrimmed response (large; includes
            HTML course descriptions).
    """
    if not duration_id:
        duration_id = _resolve_duration_id()
    resp = _fetch_classes(duration_id, marking_period_id)
    if full or resp.get("error") or not isinstance(resp.get("body"), list):
        return resp
    resp["body"] = [compact_class(c) for c in resp["body"]]
    resp["duration_id"] = duration_id
    return resp


@mcp.tool()
def gradebook(
    duration_id: int = 0,
    section_ids: list[int] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Get current-marking-period and year-to-date grades for each class.

    Calls `ParentStudentUserClassesGet` to enumerate classes in the duration,
    then `hydrategradebook` once per class to pull both the current
    marking-period grade (`SectionGrade`) and the cumulative year-to-date
    grade (`SectionGradeYear`).

    Args:
        duration_id: A DurationId from `student_terms()`. 0 / omitted =
            auto-resolve the current academic term.
        section_ids: Optional filter — only include these `lead_section_id`s.
            Pass None / omit to include all classes in the duration.
        full: True = also include the full per-section `hydrategradebook`
            response (heavy — ~30 KB per class).

    Returns body as a list of:
        {
          "section_id": int,             # lead section id
          "class": str,
          "teacher": str,
          "marking_period": str,          # e.g. "3rd Trimester"
          "marking_period_id": int,
          "graded": bool,                 # False = free period, non-graded block
          "current_grade": float | None,  # SectionGrade
          "current_grade_display": str | None,    # "85.39%"
          "year_grade": float | None,     # SectionGradeYear
          "year_grade_display": str | None,       # "90.73%"
        }
    """
    client = _get_client()
    student_id = _student_id()
    if not duration_id:
        duration_id = _resolve_duration_id()

    classes_resp = _fetch_classes(duration_id)
    if classes_resp.get("error") or not isinstance(classes_resp.get("body"), list):
        return classes_resp

    classes_list = classes_resp["body"]
    if section_ids:
        wanted = {int(s) for s in section_ids}
        classes_list = [
            c for c in classes_list if int(c.get("leadsectionid") or 0) in wanted
        ]

    out: list[dict[str, Any]] = []
    for c in classes_list:
        lead_section_id = c.get("leadsectionid")
        marking_period_id = c.get("markingperiodid")
        # Non-graded blocks (health/wellness, free periods, etc.) have no
        # markingperiodid — not an error, just nothing to fetch.
        is_graded = bool(lead_section_id and marking_period_id)
        row: dict[str, Any] = {
            "section_id": lead_section_id,
            "class": c.get("sectionidentifier"),
            "teacher": c.get("groupownername"),
            "marking_period": c.get("currentterm"),
            "marking_period_id": marking_period_id,
            "graded": is_graded,
            "current_grade": to_float(c.get("cumgrade")),
            "current_grade_display": fmt_pct(c.get("cumgrade")),
            "year_grade": None,
            "year_grade_display": None,
        }

        if not is_graded:
            out.append(row)
            continue

        hydra = client.request(
            "GET",
            "/api/gradebook/hydrategradebook",
            params={
                "sectionId": lead_section_id,
                "markingPeriodId": marking_period_id,
                "sortAssignmentId": "null",
                "sortSkillPk": "null",
                "sortDesc": "null",
                "sortCumulative": "null",
                "studentUserId": student_id,
                "fromProgress": "true",
            },
        )
        body = hydra.get("body")
        if isinstance(body, dict):
            roster = body.get("Roster") or []
            me = next(
                (
                    r
                    for r in roster
                    if str(r.get("StudentUserId") or "") == str(student_id)
                ),
                None,
            )
            if me is None and len(roster) == 1:
                me = roster[0]
            if me is not None:
                # SectionGrade from hydrategradebook is authoritative; fall
                # back to cumgrade if it's zero/null.
                sg = to_float(me.get("SectionGrade"))
                if sg is not None:
                    row["current_grade"] = sg
                    row["current_grade_display"] = fmt_pct(sg)
                row["year_grade"] = to_float(me.get("SectionGradeYear"))
                row["year_grade_display"] = fmt_pct(me.get("SectionGradeYear"))
            elif roster:
                row["error"] = "student not found in gradebook roster"
        else:
            row["error"] = f"hydrategradebook status {hydra.get('status')}"

        if full:
            row["hydrate"] = body

        out.append(row)

    return {"status": 200, "duration_id": duration_id, "body": out}


@mcp.tool()
def report_card_templates() -> dict[str, Any]:
    """List report-card templates available for the current school year."""
    return _get_client().request(
        "GET",
        "/api/Grading/StudentReportCardTemplateList",
        params={"studentId": _student_id(), "schoolYearLabel": _school_year()},
    )


@mcp.tool()
def transcript_templates() -> dict[str, Any]:
    """List transcript templates available for the current school year."""
    return _get_client().request(
        "GET",
        "/api/Grading/StudentTranscriptTemplateList",
        params={"studentId": _student_id(), "schoolYearLabel": _school_year()},
    )


@mcp.tool()
def attendance() -> dict[str, Any]:
    """Get attendance records for the current school year."""
    return _get_client().request(
        "GET",
        "/api/datadirect/ParentStudentUserAttendance/",
        params={
            "userId": _student_id(),
            "personaId": _persona_id(),
            "schoolYearLabel": _school_year(),
        },
    )


@mcp.tool()
def conduct(level_num: int = 0) -> dict[str, Any]:
    """Get conduct records.

    Args:
        level_num: Conduct level filter (0 = all).
    """
    return _get_client().request(
        "GET",
        "/api/datadirect/ParentStudentUserConduct/",
        params={
            "studentUserId": _student_id(),
            "viewerPersonaId": _persona_id(),
            "schoolYearLabel": _school_year(),
            "levelNum": level_num,
        },
    )


@mcp.tool()
def grade_levels() -> dict[str, Any]:
    """List the student's grade-level history."""
    return _get_client().request("GET", "/api/datadirect/StudentGradeLevelList/")


# ---------------------------------------------------------------------------
# Groups (advisory / athletic / dorm / activity / community)
# ---------------------------------------------------------------------------


_GROUP_ENDPOINTS: dict[str, tuple[str, str, int]] = {
    "advisory": ("ParentStudentUserAdvisoryGroupsGet", "durationId", 3),
    "athletic": ("ParentStudentUserAthleticGroupsGet", "durationList", 9),
    "dorm": ("ParentStudentUserDormGroupsGet", "durationId", 4),
    "activity": ("ParentStudentUserActivityGroupsGet", "durationId", 2),
    "community": ("ParentStudentUserCommunityGroupsGet", "durationId", 11),
}


@mcp.tool()
def group_membership(kind: str, duration_id: int = 0) -> dict[str, Any]:
    """List the student's group memberships of a given kind.

    Args:
        kind: One of: advisory, athletic, dorm, activity, community.
        duration_id: A DurationId from `student_terms()`. 0 / omitted =
            auto-resolve the current term for this kind of group.
            Community defaults to 0 (school-wide), without a term lookup.
    """
    if kind not in _GROUP_ENDPOINTS:
        raise ValueError(
            f"Unknown kind '{kind}'. Valid: {', '.join(sorted(_GROUP_ENDPOINTS))}."
        )
    endpoint, dur_param, offering_type = _GROUP_ENDPOINTS[kind]
    if not duration_id and kind != "community":
        duration_id = _resolve_duration_id(offering_type)
    return _get_client().request(
        "GET",
        f"/api/datadirect/{endpoint}",
        params={
            "userId": _student_id(),
            "schoolYearLabel": _school_year(),
            "memberLevel": 3,
            "persona": _persona_id(),
            dur_param: duration_id,
        },
    )


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


@mcp.tool()
def calendar_list(
    date_start: str,
    date_end: str,
    calendar_set_id: int = 1,
    settings_type_id: int = 1,
) -> dict[str, Any]:
    """List the user's calendar *definitions* (not events).

    Despite taking a date range, this endpoint returns the set of calendars
    visible to the user (Assignments, Schedule, Games/Practices, school
    calendars, ...) with their colors and per-group Filters[] — it's what
    the SPA uses to draw the calendar sidebar. Use `schedule` /
    `assignments` for actual day-to-day items, or `api_request` against
    other /api/mycalendar/ endpoints for raw events.

    Args:
        date_start: YYYY-MM-DD.
        date_end: YYYY-MM-DD.
        calendar_set_id: Which calendar set (default 1 = main).
        settings_type_id: Settings/filter profile (default 1).
    """
    return _get_client().request(
        "GET",
        "/api/mycalendar/list/",
        params={
            "startDate": mdy(date_start),
            "endDate": mdy(date_end),
            "settingsTypeId": settings_type_id,
            "calendarSetId": calendar_set_id,
            "recentFilterSave": "false",
        },
    )


@mcp.tool()
def calendar_actions(calendar_set_id: int = 1) -> dict[str, Any]:
    """Get calendar metadata (available filters and feeds)."""
    return _get_client().request(
        "GET",
        "/api/mycalendar/actions/",
        params={"calendarSetId": calendar_set_id},
    )


# ---------------------------------------------------------------------------
# Official notes (inbox)
# ---------------------------------------------------------------------------


@mcp.tool()
def official_notes(
    to_date: str | None = None,
    category_id: int = 22,
    current_only: int = 1,
    sort_by: int = 1,
    search_text: str = "",
) -> dict[str, Any]:
    """List official notes from the school (the inbox view).

    Args:
        to_date: Latest date YYYY-MM-DD. Defaults to today.
        category_id: Note category id. School-specific — 22 is the value
            observed on the deployment this server was built against; find
            yours in DevTools on the site's Notes/Inbox page.
        current_only: 1 for current, 0 for archived.
        sort_by: Sort key.
        search_text: Free-text search.
    """
    if not to_date:
        to_date = date.today().isoformat()
    return _get_client().request(
        "GET",
        "/api/officialnote/InboxExternal/",
        params={
            "format": "json",
            "currentInd": current_only,
            "statusXml": "",
            "commentTypeXml": "",
            "fromDate": "",
            "toDate": mdy(to_date),
            "searchText": search_text,
            "studentUserId": "",
            "categoryId": category_id,
            "fromParentUnread": "false",
            "sortBy": sort_by,
        },
    )


@mcp.tool()
def official_note_types(category_id: int = 22) -> dict[str, Any]:
    """List available note types for a category (see `official_notes`)."""
    return _get_client().request(
        "GET",
        "/api/datadirect/OfficialNoteTypeGet/",
        params={"format": "json", "status": 0, "categoryId": category_id},
    )


# ---------------------------------------------------------------------------
# News / activity feed
# ---------------------------------------------------------------------------


@mcp.tool()
def activity_feed(last_date_ticks: str = "") -> dict[str, Any]:
    """Get the activity feed (news, posts, school updates).

    Args:
        last_date_ticks: .NET ticks of the oldest item seen so far, for
            pagination. Empty string for first page.
    """
    return _get_client().request(
        "GET",
        "/api/datadirect/ActivityFeedGet/",
        params={"format": "json", "lastDate": last_date_ticks},
    )


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------


@mcp.tool()
def directory_search(
    directory_id: int,
    query: str = "",
    facets: str = "",
    search_all: bool = False,
    limit: int = 25,
    full: bool = False,
) -> dict[str, Any]:
    """Search a school directory.

    Results are compacted (user_id, name, email, phone, job_title,
    department, grad_year, grade, is_student) and capped at `limit` — an
    empty query makes the raw endpoint return the *entire* directory, which
    can be hundreds of entries / 100+ KB. The response includes
    `total_results` so you can tell when the cap kicked in.

    Args:
        directory_id: Numeric directory id (school-specific; e.g. 397 =
            Faculty/Staff on the deployment this was built against). Find
            yours via `directory_info` / DevTools.
        query: Free-text search.
        facets: Encoded facet filter from `directory_facets()`.
        search_all: Match across all facets.
        limit: Max results to return (default 25).
        full: True = raw, uncapped endpoint response (can be huge).
    """
    resp = _get_client().request(
        "GET",
        "/api/directory/directoryresultsget",
        params={
            "directoryId": directory_id,
            "searchVal": query,
            "facets": facets,
            "searchAll": "true" if search_all else "false",
        },
    )
    if full or resp.get("error") or not isinstance(resp.get("body"), list):
        return resp
    rows = resp["body"]
    limit = max(1, limit)
    resp["body"] = [compact_directory_entry(r) for r in rows[:limit]]
    resp["total_results"] = len(rows)
    if len(rows) > limit:
        resp["truncated_to"] = limit
    return resp


@mcp.tool()
def directory_info(directory_id: int) -> dict[str, Any]:
    """Get directory metadata (columns, permissions)."""
    return _get_client().request(
        "GET",
        "/api/directory/directoryget",
        params={"directoryId": directory_id},
    )


@mcp.tool()
def directory_facets(directory_id: int) -> dict[str, Any]:
    """Get available filter facets for a directory."""
    return _get_client().request(
        "GET",
        "/api/directory/directoryfacetsget",
        params={"directoryId": directory_id},
    )


# ---------------------------------------------------------------------------
# Escape hatch
# ---------------------------------------------------------------------------


@mcp.tool()
def api_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    form_data: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call any myschoolapp endpoint with the authenticated session.

    Use for anything not covered by a typed tool. Discover endpoints with
    DevTools > Network tab while using the site in a browser.

    Requests are pinned to the school's own host — absolute URLs pointing
    anywhere else are rejected so the session cookie can't leak.

    Args:
        method: HTTP method.
        path: Path starting with `/`.
        params: Query string parameters.
        json_body: JSON body for POST/PUT/PATCH.
        form_data: Form-encoded body.
        extra_headers: Additional headers (e.g. X-CSRF-Token).
    """
    return _get_client().request(
        method,
        path,
        params=params,
        json_body=json_body,
        data=form_data,
        extra_headers=extra_headers,
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
