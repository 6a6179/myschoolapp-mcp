"""MCP server exposing myschoolapp.com via FastMCP.

Endpoints were mapped by capturing live network traffic from a logged-in
student session on Tabor Academy's myschoolapp deployment. They follow the
standard Blackbaud onMSA SPA conventions and should work on any school's
instance, though some IDs (categoryId, durationId, directoryId) are
school-specific.
"""

from __future__ import annotations

import asyncio
import html as _html
import json as _json
import os
import re as _re
from datetime import date, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import MyschoolappClient, load_env_file

_ENV_PATH = load_env_file()

mcp = FastMCP("myschoolapp")
_client: MyschoolappClient | None = None


def _get_client() -> MyschoolappClient:
    global _client
    if _client is None:
        _client = MyschoolappClient()
    return _client


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


def _mdy(d: str | None) -> str:
    if not d:
        return ""
    y, m, day = d.split("-")
    return f"{int(m)}/{int(day)}/{y}"


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


@mcp.tool()
def whoami() -> dict[str, Any]:
    """Verify session cookie and return current user's profile tabs.

    If this returns HTML or 401/403, the cookie is expired or wrong.
    """
    return _get_client().request(
        "GET",
        "/api/user/profiletabs",
        params={"showuserid": _student_id(), "personaId": _persona_id()},
    )


@mcp.tool()
def config() -> dict[str, str]:
    """Show resolved config (subdomain, student id, persona, school year)."""
    client = _get_client()
    return {
        "subdomain": client.subdomain or "",
        "student_id": _student_id(),
        "persona_id": _persona_id(),
        "school_year": _school_year(),
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

    global _client
    # refresh_cookie() uses Playwright's sync API, which cannot run inside
    # the asyncio event loop FastMCP runs us in. Offload to a worker thread.
    try:
        path = await asyncio.to_thread(refresh_cookie)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None

    return {"ok": True, "cookie_path": str(path)}


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


_ASSIGNMENT_BUCKETS = (
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

_DEFAULT_ASSIGNMENT_BUCKETS = (
    "Missing",
    "Overdue",
    "DueToday",
    "DueTomorrow",
    "DueThisWeek",
    "DueNextWeek",
    "DueAfterNextWeek",
    "PastThisWeek",
    "PastLastWeek",
)

_ASSIGNMENT_ITEM_FIELDS = (
    "AssignmentId",
    "AssignmentIndexId",
    "SectionId",
    "GroupName",
    "ShortDescription",
    "AssignmentType",
    "DateAssigned",
    "DateDue",
    "MaxPoints",
    "StudentStatus",
    "AssignmentStatus",
    "MissingInd",
    "LateInd",
    "IncompleteInd",
    "Major",
    "ExtraCredit",
    "MarkingPeriodDescription",
    "MarkingPeriodId",
    "HasGrade",
    "DropBoxInd",
    "DropBoxToDo",
)


def _compact_assignment(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item.get(k) for k in _ASSIGNMENT_ITEM_FIELDS if k in item}


@mcp.tool()
def assignments(
    display_by_due_date: bool = True,
    buckets: str = "",
    full: bool = False,
) -> dict[str, Any]:
    """Get assignments grouped by due-date bucket.

    Available buckets: Missing, Overdue, DueToday, DueTomorrow, DueThisWeek,
    DueNextWeek, DueAfterNextWeek, PastThisWeek, PastLastWeek,
    PastBeforeLastWeek. PastBeforeLastWeek is excluded by default because it
    typically contains hundreds of items.

    Each compact item includes GroupName (class), ShortDescription, DateDue,
    DateAssigned, AssignmentType, MaxPoints, StudentStatus (decode with
    `assignment_status_labels()`), MissingInd, LateInd, Major, ExtraCredit,
    MarkingPeriodDescription, HasGrade.

    Args:
        display_by_due_date: True = group by due date (default).
            False = group by class section.
        buckets: Comma-separated bucket names to include. Empty = default
            set (everything except PastBeforeLastWeek). Use "all" to include
            every bucket.
        full: True = return the raw, untrimmed response (large — likely to
            exceed token limits).
    """
    resp = _get_client().request(
        "GET",
        "/api/assignment2/StudentAssignmentCenterGet",
        params={"displayByDueDate": "true" if display_by_due_date else "false"},
    )
    if full or not isinstance(resp.get("body"), dict):
        return resp

    body = resp["body"]
    if not buckets:
        wanted = set(_DEFAULT_ASSIGNMENT_BUCKETS)
    elif buckets.strip().lower() == "all":
        wanted = set(_ASSIGNMENT_BUCKETS)
    else:
        wanted = {b.strip() for b in buckets.split(",") if b.strip()}

    out_buckets: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for name in _ASSIGNMENT_BUCKETS:
        items = body.get(name) or []
        counts[name] = len(items)
        if name in wanted:
            out_buckets[name] = [_compact_assignment(i) for i in items]

    sections = [
        {k: s.get(k) for k in ("LeadSectionId", "GroupName") if k in s}
        for s in (body.get("Sections") or [])
    ]
    major = [_compact_assignment(i) for i in (body.get("MajorAssignments") or [])]

    return {
        "status": resp.get("status"),
        "url": resp.get("url"),
        "counts": counts,
        "buckets_included": sorted(out_buckets.keys()),
        "buckets": out_buckets,
        "sections": sections,
        "major_assignments": major,
    }


@mcp.tool()
def assignment_status_labels() -> dict[str, str]:
    """Map StudentStatus codes returned by `assignments` to readable labels."""
    return {
        "1": "Completed",
        "0": "In progress",
        "2": "Overdue",
        "-2147483648": "To do",
    }


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
    need a custom date window.

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
            "dateStart": _mdy(date_start),
            "dateEnd": _mdy(date_end),
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


# AssignmentStatusType enum, decoded from the lms-assignment SPA bundle.
# -2147483648 is .NET int.MinValue and serves as a "not set" sentinel that
# the SPA also treats as ToDo.
_STATUS_TYPE_LABELS: dict[int, str] = {
    -2147483648: "To do",
    -1: "To do",
    0: "In progress",
    1: "Completed",
    2: "Overdue",
    3: "Retake",
    4: "Graded",
    6: "Paused",
}


def _status_label(t: Any) -> str | None:
    if t is None:
        return None
    try:
        return _STATUS_TYPE_LABELS.get(int(t))
    except (TypeError, ValueError):
        return None


_HTML_BLOCK_TAGS = _re.compile(
    r"</\s*(p|div|li|h[1-6]|tr|blockquote|pre)\s*>", _re.IGNORECASE
)
_HTML_BREAK_TAGS = _re.compile(r"<\s*br\s*/?\s*>", _re.IGNORECASE)
_HTML_LIST_ITEM = _re.compile(r"<\s*li[^>]*>", _re.IGNORECASE)
_HTML_LINK = _re.compile(
    r'<\s*a\b[^>]*?href\s*=\s*"([^"]+)"[^>]*>(.*?)</\s*a\s*>',
    _re.IGNORECASE | _re.DOTALL,
)
_HTML_TAG = _re.compile(r"<[^>]+>")
_WS_RUN = _re.compile(r"[ \t]+")
_NL_RUN = _re.compile(r"\n{3,}")


def _strip_html(s: Any) -> str | None:
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
    out = _html.unescape(out)
    out = _WS_RUN.sub(" ", out)
    out = "\n".join(line.rstrip() for line in out.splitlines())
    out = _NL_RUN.sub("\n\n", out).strip()
    return out or None


def _absolute_url(client: MyschoolappClient, url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        return f"{client.base_url}{url}"
    return f"{client.base_url}/{url}"


def _format_range(low: Any, high: Any) -> str | None:
    """Render a rubric level point range like '3.1-4' or just '4'."""
    if low is None and high is None:
        return None
    if low is None:
        return str(high)
    if high is None or low == high:
        return str(low)
    return f"{low}-{high}"


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


def _clean_download(client: MyschoolappClient, d: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": d.get("FriendlyFileName") or d.get("ShortDescription"),
        "url": _absolute_url(client, d.get("DownloadUrl")),
    }


def _clean_link(client: MyschoolappClient, l: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": l.get("ShortDescription") or l.get("FriendlyFileName") or l.get("Url"),
        "url": _absolute_url(client, l.get("Url") or l.get("DownloadUrl")),
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
    `41609904`. This is the per-student index id, NOT the global AssignmentId
    returned by `assignments()`. Use the AssignmentIndexId field from the
    assignments list, or grab it from the browser URL.

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
        "status": _status_label(grade.get("AssignmentStatusType")),
        "grade": grade.get("Grade") or None,
        "comment": grade.get("GradedComment") or None,
        "graded": bool(grade.get("HasGrade")),
        "late": bool(grade.get("Late")),
        "missing": bool(grade.get("Missing")),
        "exempt": bool(grade.get("Exempt")),
        "incomplete": bool(grade.get("Incomplete")),
        "collected": bool(grade.get("Collected")),
        "description": _strip_html(body.get("LongDescription")),
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
            _clean_download(client, d) for d in (body.get("DownloadItems") or [])
        ],
        "links": [_clean_link(client, l) for l in (body.get("LinkItems") or [])],
    }

    out: dict[str, Any] = {"assignment": cleaned}

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
                "description": _strip_html(rb_body.get("Description")),
                "criteria": [
                    {
                        "name": skl.get("Name"),
                        "description": _strip_html(skl.get("Description")),
                        "levels": [
                            {
                                "name": lv.get("Name"),
                                "description": _strip_html(lv.get("Description")),
                                "points": _format_range(
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
                    "comment": _strip_html(r.get("Comment")) or None,
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
def schedule(on_date: str | None = None) -> dict[str, Any]:
    """Get the day's class schedule.

    Args:
        on_date: YYYY-MM-DD. Empty/None = today.
    """
    return _get_client().request(
        "GET",
        "/api/schedule/MyDayCalendarStudentList/",
        params={"scheduleDate": _mdy(on_date), "personaId": _persona_id()},
    )


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
            "mydayDate": _mdy(on_date),
            "viewerId": _student_id(),
            "viewerPersonaId": _persona_id(),
        },
    )


# ---------------------------------------------------------------------------
# Academics
# ---------------------------------------------------------------------------


@mcp.tool()
def student_terms() -> dict[str, Any]:
    """List academic groups and their terms (durations) for the school year.

    The response contains DurationId values needed by `classes`, `gradebook`,
    `group_membership`, etc.
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


_CLASS_FIELDS = (
    "sectionid",
    "leadsectionid",
    "sectionidentifier",
    "room",
    "currentterm",
    "DurationId",
    "markingperiodid",
    "groupownername",
    "groupowneremail",
    "OwnerId",
    "cumgrade",
    "CumulativeDisplay",
    "OverdueCount",
    "UpcomingCount",
    "assignmentactivetoday",
    "assignmentduetoday",
    "assignmentassignedtoday",
    "canviewassignments",
    "publishgrouptouser",
    "AttendanceTaken",
)


@mcp.tool()
def classes(
    duration_id: int, marking_period_id: str = "", full: bool = False
) -> dict[str, Any]:
    """List classes for a given term (duration).

    By default returns a compact view with the section info, teacher, room,
    cumulative grade, and assignment counts. Course descriptions, photos,
    and other heavy fields are stripped — set ``full=True`` to keep them.

    Args:
        duration_id: A DurationId from `student_terms()`.
        marking_period_id: Optional marking-period filter.
        full: True = return the raw, untrimmed response (large; includes
            HTML course descriptions).
    """
    resp = _get_client().request(
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
    if full or not isinstance(resp.get("body"), list):
        return resp
    resp["body"] = [
        {k: c.get(k) for k in _CLASS_FIELDS if k in c} for c in resp["body"]
    ]
    return resp


def _fmt_pct(n: Any) -> str | None:
    """Format a number like 85.39 as '85.39%'. Returns None for empty/zero."""
    if n is None or n == "":
        return None
    try:
        f = float(n)
    except (TypeError, ValueError):
        return None
    # treat exact 0 as "no grade yet" rather than "0%"
    if f == 0:
        return None
    return f"{f:.2f}%"


def _to_float(n: Any) -> float | None:
    if n is None or n == "":
        return None
    try:
        f = float(n)
    except (TypeError, ValueError):
        return None
    return f if f != 0 else None


@mcp.tool()
def gradebook(
    duration_id: int,
    section_ids: list[int] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Get current-marking-period and year-to-date grades for each class.

    Calls `ParentStudentUserClassesGet` to enumerate classes in the duration,
    then `hydrategradebook` once per class to pull both the current
    marking-period grade (`SectionGrade`) and the cumulative year-to-date
    grade (`SectionGradeYear`).

    Args:
        duration_id: A DurationId from `student_terms()`.
        section_ids: Optional filter — only include these `leadsectionid`s.
            Pass None / omit to include all classes in the duration.
        full: True = also include the full per-section `hydrategradebook`
            response (heavy — ~30 KB per class).

    Returns body as a list of:
        {
          "section_id": int,             # leadsectionid
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

    classes_resp = client.request(
        "GET",
        "/api/datadirect/ParentStudentUserClassesGet",
        params={
            "userId": student_id,
            "schoolYearLabel": _school_year(),
            "memberLevel": 3,
            "persona": _persona_id(),
            "durationList": duration_id,
            "markingPeriodId": "",
        },
    )
    if not isinstance(classes_resp.get("body"), list):
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
            "current_grade": _to_float(c.get("cumgrade")),
            "current_grade_display": _fmt_pct(c.get("cumgrade")),
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
            if roster:
                me = roster[0]
                # SectionGrade from hydrategradebook is authoritative; fall back
                # to cumgrade if it's zero/null.
                sg = _to_float(me.get("SectionGrade"))
                if sg is not None:
                    row["current_grade"] = sg
                    row["current_grade_display"] = _fmt_pct(sg)
                row["year_grade"] = _to_float(me.get("SectionGradeYear"))
                row["year_grade_display"] = _fmt_pct(me.get("SectionGradeYear"))
        else:
            row["error"] = f"hydrategradebook status {hydra.get('status')}"

        if full:
            row["hydrate"] = body

        out.append(row)

    return {"status": 200, "body": out}


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


_GROUP_ENDPOINTS: dict[str, tuple[str, str]] = {
    "advisory": ("ParentStudentUserAdvisoryGroupsGet", "durationId"),
    "athletic": ("ParentStudentUserAthleticGroupsGet", "durationList"),
    "dorm": ("ParentStudentUserDormGroupsGet", "durationId"),
    "activity": ("ParentStudentUserActivityGroupsGet", "durationId"),
    "community": ("ParentStudentUserCommunityGroupsGet", "durationId"),
}


@mcp.tool()
def group_membership(kind: str, duration_id: int = 0) -> dict[str, Any]:
    """List the student's group memberships of a given kind.

    Args:
        kind: One of: advisory, athletic, dorm, activity, community.
        duration_id: A DurationId from `student_terms()`. Use 0 for community
            (school-wide).
    """
    if kind not in _GROUP_ENDPOINTS:
        return {
            "error": True,
            "message": f"Unknown kind '{kind}'. Valid: {sorted(_GROUP_ENDPOINTS)}",
        }
    endpoint, dur_param = _GROUP_ENDPOINTS[kind]
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
    """List calendar items in a date range.

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
            "startDate": _mdy(date_start),
            "endDate": _mdy(date_end),
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
        category_id: Note category id (school-specific; 22 observed default).
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
            "toDate": _mdy(to_date),
            "searchText": search_text,
            "studentUserId": "",
            "categoryId": category_id,
            "fromParentUnread": "false",
            "sortBy": sort_by,
        },
    )


@mcp.tool()
def official_note_types(category_id: int = 22) -> dict[str, Any]:
    """List available note types for a category."""
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
) -> dict[str, Any]:
    """Search a directory.

    Args:
        directory_id: Numeric directory id (e.g. 397 for Faculty/Staff at Tabor).
        query: Free-text search.
        facets: Encoded facet filter from `directory_facets()`.
        search_all: Match across all facets.
    """
    return _get_client().request(
        "GET",
        "/api/directory/directoryresultsget",
        params={
            "directoryId": directory_id,
            "searchVal": query,
            "facets": facets,
            "searchAll": "true" if search_all else "false",
        },
    )


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
