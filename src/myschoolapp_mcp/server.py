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
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from .client import MyschoolappClient, RequestBoundaryError, load_env_file
from .errors import SessionError, UserError
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
            _install_refresh_hook(_client)
        return _client


def _auto_refresh_enabled() -> bool:
    return os.environ.get("MSA_AUTO_REFRESH", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _install_refresh_hook(client: MyschoolappClient) -> None:
    """Let the client re-login once on an expired session, if opted in.

    MSA_AUTO_REFRESH=true plus SCHOOL_EMAIL/SCHOOL_PASS are required; the
    hook runs the same Playwright flow as cookie_refresh, on the calling
    thread (tools are sync and the SDK already offloads them).
    """
    if not _auto_refresh_enabled():
        return
    if not (os.environ.get("SCHOOL_EMAIL") and os.environ.get("SCHOOL_PASS")):
        return

    def hook() -> dict[str, str]:
        from .auth import refresh_cookie
        from .client import _load_cookies_from_file

        return _load_cookies_from_file(refresh_cookie())

    client.refresh_hook = hook


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
        raise SessionError(
            "Set MSA_STUDENT_ID to your numeric persona user id. Find it in "
            "any /api/user/profiletabs?showuserid=... request in DevTools."
        )
    return sid


def _persona_id() -> str:
    return os.environ.get("MSA_PERSONA_ID", "2")  # 2 = student


def _school_timezone() -> ZoneInfo:
    name = os.environ.get("MSA_TIMEZONE", "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise UserError(
            f"Invalid MSA_TIMEZONE {name!r}. Use an available IANA timezone "
            "such as 'America/New_York' or 'UTC' (default)."
        ) from None


def _today() -> date:
    return datetime.now(_school_timezone()).date()


def _school_year() -> str:
    sy = os.environ.get("MSA_SCHOOL_YEAR")
    if sy:
        return sy
    today = _today()
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
    """Show resolved config (school, student, persona, school year, timezone)."""
    client = _get_client()
    return {
        "subdomain": client.subdomain or "",
        "student_id": os.environ.get("MSA_STUDENT_ID")
        or "(unset — set MSA_STUDENT_ID; most tools will fail without it)",
        "persona_id": _persona_id(),
        "school_year": _school_year(),
        "timezone": _school_timezone().key,
        "env_file": str(_ENV_PATH) if _ENV_PATH else None,
        "auto_refresh": client.refresh_hook is not None,
        "api_request_writes": _api_writes_allowed(),
    }


@mcp.tool()
async def cookie_refresh() -> dict[str, Any]:
    """Re-run the Playwright login flow to refresh the session cookie.

    Drives a fresh Microsoft OAuth login using SCHOOL_EMAIL / SCHOOL_PASS
    from the environment, writes the new cookie to MSA_COOKIES_FILE (or the
    default ~/.myschoolapp-mcp/cookie.txt), then replaces the cached HTTP
    client using that file even when MSA_COOKIE is set. A failed refresh
    keeps the existing client; environment defaults remain unchanged.

    Use this when other tools start returning HTML or 401/403 — i.e. when
    the cookie has expired.

    Takes roughly 10-30 seconds. Requires playwright + chromium installed
    on the host. Does not work on 2FA / MFA accounts. On a server with a
    new IP, Microsoft may demand additional verification and the flow will
    fail; in that case, refresh on a trusted machine and copy the cookie
    file over.
    """
    global _client
    from .auth import refresh_cookie
    from .client import _load_cookies_from_file

    # refresh_cookie() uses Playwright's sync API, which cannot run inside
    # the asyncio event loop the MCP server runs us in. Offload to a worker
    # thread.
    path = await asyncio.to_thread(refresh_cookie)
    fresh_client = MyschoolappClient(cookies=_load_cookies_from_file(path))
    _install_refresh_hook(fresh_client)
    with _client_lock:
        previous_client = _client
        _client = fresh_client
        if previous_client is not None:
            with contextlib.suppress(Exception):
                previous_client.close()
    return {"ok": True, "cookie_path": str(path)}


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


@mcp.tool()
def assignments(
    display_by_due_date: bool = True,
    buckets: str = "",
    full: bool = False,
    days_ahead: int = 60,
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
    it widens the fetch window to ~6 months back. The forward window is
    `days_ahead` days (default 60), so DueAfterNextWeek only covers that
    horizon — raise days_ahead for a full-semester view. Missing/Overdue
    come from the server's missing/overdue filter; items flagged there are
    removed from the date buckets so nothing is listed twice.

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
        days_ahead: How far forward to fetch (1-365, default 60).
    """
    if not 1 <= days_ahead <= 365:
        raise UserError("days_ahead must be between 1 and 365.")
    if not buckets:
        wanted = set(DEFAULT_ASSIGNMENT_BUCKETS)
    elif buckets.strip().lower() == "all":
        wanted = set(ASSIGNMENT_BUCKETS)
    else:
        wanted = {b.strip() for b in buckets.split(",") if b.strip()}
        unknown = wanted - set(ASSIGNMENT_BUCKETS)
        if unknown:
            raise UserError(
                f"Unknown bucket(s) {sorted(unknown)}. "
                f"Valid: {', '.join(ASSIGNMENT_BUCKETS)} (or 'all')."
            )

    client = _get_client()
    today = _today()
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
            "dateEnd": mdy((today + timedelta(days=days_ahead)).isoformat()),
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
        "window": {
            "start": (today - timedelta(days=days_back)).isoformat(),
            "end": (today + timedelta(days=days_ahead)).isoformat(),
        },
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
    if not date_start or not date_end:
        today = _today()
        if not date_start:
            date_start = today.isoformat()
        if not date_end:
            date_end = (today + timedelta(days=30)).isoformat()
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
    if urlsplit(url).scheme:
        return url
    return urljoin(f"{client.base_url}/", url)


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


def _response_succeeded(response: dict[str, Any]) -> bool:
    status = response.get("status")
    return isinstance(status, int) and 200 <= status < 300 and not response.get("error")


def _request_assignment_component(
    client: MyschoolappClient, path: str, params: dict[str, Any]
) -> dict[str, Any]:
    try:
        return client.request("GET", path, params=params)
    except (httpx.HTTPError, RuntimeError, RequestBoundaryError) as exc:
        return {
            "status": None,
            "error": f"Component transport failure: {type(exc).__name__}",
            "url": path,
            "body": None,
        }


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

    if not _response_succeeded(detail):
        return {"detail": detail}

    rubric_resp: dict[str, Any] | None = None
    rubric_results_resp: dict[str, Any] | None = None
    if include_rubric:
        body = detail.get("body") if isinstance(detail, dict) else None
        rubric_id = body.get("RubricId") if isinstance(body, dict) else None
        try:
            rubric_id = int(rubric_id)
        except (TypeError, ValueError):
            rubric_id = 0
        if rubric_id > 0:
            rubric_resp = _request_assignment_component(
                client,
                "/api/Rubric/AssignmentRubric/",
                params={"id": str(rubric_id)},
            )
            rubric_results_resp = _request_assignment_component(
                client,
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
        if _response_succeeded(rubric_resp) and isinstance(rb_body, dict):
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

        if not _response_succeeded(rubric_results_resp):
            rubric_out["results_error"] = rubric_results_resp
        elif isinstance(results_body, list):
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
            rubric_out["results_error"] = rubric_results_resp

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
        params={
            "scheduleDate": mdy(on_date or _today().isoformat()),
            "personaId": _persona_id(),
        },
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
            "mydayDate": mdy(on_date or _today().isoformat()),
            "viewerId": _student_id(),
            "viewerPersonaId": _persona_id(),
        },
    )


# ---------------------------------------------------------------------------
# Academics
# ---------------------------------------------------------------------------


@mcp.tool()
def student_terms(school_year: str | None = None) -> dict[str, Any]:
    """List the student's terms (durations) for the school year.

    Each row has DurationId + DurationDescription (e.g. "Year Long",
    "Fall Season"), OfferingType (1 = academics, 2 = activities,
    3 = advisory, 4 = dorm, 9 = athletics, 11 = community), and CurrentInd
    (1 = term is currently active). DurationId values feed `classes`,
    `gradebook`, and `group_membership`; those tools auto-resolve the
    current term for their offering type when omitted. Community uses 0.

    Args:
        school_year: Exact label from `school_years()`. None uses
            MSA_SCHOOL_YEAR or the date-derived school year.
    """
    return _get_client().request(
        "GET",
        "/api/DataDirect/StudentGroupTermList/",
        params={
            "studentUserId": _student_id(),
            "schoolYearLabel": _school_year() if school_year is None else school_year,
            "personaId": _persona_id(),
        },
    )


def _resolve_duration_id(offering_type: int = 1, school_year: str | None = None) -> int:
    """Pick the current term for an offering type (academics by default)."""
    resp = student_terms() if school_year is None else student_terms(school_year)
    body = resp.get("body")
    if not _response_succeeded(resp) or not isinstance(body, list):
        raise SessionError(
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
        raise SessionError(
            f"No currently-active term for OfferingType {offering_type} found. "
            "Pass duration_id explicitly — see student_terms()."
        )
    return int(current[0]["DurationId"])


def _fetch_classes(
    duration_id: int, marking_period_id: str = "", school_year: str | None = None
) -> dict[str, Any]:
    return _get_client().request(
        "GET",
        "/api/datadirect/ParentStudentUserClassesGet",
        params={
            "userId": _student_id(),
            "schoolYearLabel": _school_year() if school_year is None else school_year,
            "memberLevel": 3,
            "persona": _persona_id(),
            "durationList": duration_id,
            "markingPeriodId": marking_period_id,
        },
    )


@mcp.tool()
def classes(
    duration_id: int = 0,
    marking_period_id: str = "",
    full: bool = False,
    school_year: str | None = None,
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
        school_year: Exact label from `school_years()`. None uses the configured
            or date-derived year. If that year has no current term, supply a
            duration_id from `student_terms(school_year=...)`.
    """
    if not duration_id:
        duration_id = (
            _resolve_duration_id()
            if school_year is None
            else _resolve_duration_id(school_year=school_year)
        )
    resp = (
        _fetch_classes(duration_id, marking_period_id)
        if school_year is None
        else _fetch_classes(duration_id, marking_period_id, school_year=school_year)
    )
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
    school_year: str | None = None,
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
        school_year: Optional exact school-year label. Historical years
            without an active term require an explicit duration_id.

    Returns body as a list of:
        {
          "section_id": int,             # lead section id
          "class": str,
          "teacher": str,
          "marking_period": str,          # e.g. "3rd Trimester"
          "marking_period_id": int,
          "graded": bool,                 # has usable gradebook identifiers
          "current_grade": float | None,  # SectionGrade
          "current_grade_display": str | None,    # "85.39%"
          "year_grade": float | None,     # SectionGradeYear
          "year_grade_display": str | None,       # "90.73%"
        }
    """
    client = _get_client()
    student_id = _student_id()
    if not duration_id:
        duration_id = (
            _resolve_duration_id()
            if school_year is None
            else _resolve_duration_id(school_year=school_year)
        )

    classes_resp = (
        _fetch_classes(duration_id)
        if school_year is None
        else _fetch_classes(duration_id, school_year=school_year)
    )
    if not _response_succeeded(classes_resp) or not isinstance(
        classes_resp.get("body"), list
    ):
        return classes_resp

    classes_list = classes_resp["body"]
    if section_ids is not None:
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
        class_grade = compact_class(c).get("current_grade")
        row: dict[str, Any] = {
            "section_id": lead_section_id,
            "class": c.get("sectionidentifier"),
            "teacher": c.get("groupownername"),
            "marking_period": c.get("currentterm"),
            "marking_period_id": marking_period_id,
            "graded": is_graded,
            "hydration_verified": False,
            "current_grade": class_grade,
            "current_grade_display": fmt_pct(class_grade),
            "year_grade": None,
            "year_grade_display": None,
        }

        if not is_graded:
            out.append(row)
            continue

        try:
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
        except (httpx.HTTPError, RuntimeError, RequestBoundaryError) as exc:
            hydra = {
                "status": None,
                "error": f"hydrategradebook transport failure: {type(exc).__name__}",
                "body": None,
            }
        body = hydra.get("body")
        if _response_succeeded(hydra) and isinstance(body, dict):
            roster = body.get("Roster")
            if not isinstance(roster, list):
                roster = []
            me = next(
                (
                    r
                    for r in roster
                    if isinstance(r, dict)
                    and str(r.get("StudentUserId")) == str(student_id)
                ),
                None,
            )
            if me is not None:
                row["hydration_verified"] = True
                # SectionGrade from hydrategradebook is authoritative; fall
                # back to cumgrade only if it's missing, preserving real zero.
                sg = to_float(me.get("SectionGrade"))
                if sg is not None:
                    row["current_grade"] = sg
                    row["current_grade_display"] = fmt_pct(sg)
                # This endpoint uses year zero as an unpublished-grade marker;
                # unlike current grades, no year display field disambiguates it.
                year_grade = to_float(me.get("SectionGradeYear"))
                row["year_grade"] = None if year_grade == 0 else year_grade
                row["year_grade_display"] = fmt_pct(row["year_grade"])
            else:
                row["error"] = "student not found in gradebook roster"
                row["hydrate_error"] = hydra
        else:
            row["error"] = f"hydrategradebook status {hydra.get('status')}"
            row["hydrate_error"] = hydra

        if full:
            row["hydrate"] = body

        out.append(row)

    result = {"status": 200, "duration_id": duration_id, "body": out}
    failures = [row for row in out if row.get("error")]
    if failures:
        partial = len(failures) < len(out)
        # `status` here is a synthetic summary code for this tool's wrapper,
        # not an HTTP status from the school: 207 = some sections failed,
        # 502 = every graded section failed.
        result.update(
            status=207 if partial else 502,
            status_source="synthetic",
            error=f"Could not verify gradebook for {len(failures)} section(s)",
            partial=partial,
        )
    return result


@mcp.tool()
def report_card_templates(school_year: str | None = None) -> dict[str, Any]:
    """List modern report-card templates, falling back to legacy reports.

    Only a successful (2xx), error-free empty modern list triggers the
    legacy performance endpoint. Its successful list is filtered to
    performance_type == "Report" and tagged source="legacy"; legacy IDs
    are not modern template IDs. Preserve the response wrapper fields.
    Nonempty modern results, errors, and non-list responses are unchanged.

    Args:
        school_year: School-year label (e.g. "2025 - 2026"). Defaults to
            MSA_SCHOOL_YEAR or the current year derived by _school_year().
    """
    client = _get_client()
    school_year = _school_year() if school_year is None else school_year
    response = client.request(
        "GET",
        "/api/Grading/StudentReportCardTemplateList",
        params={"studentId": _student_id(), "schoolYearLabel": school_year},
    )
    if (
        not 200 <= response.get("status", 0) < 300
        or response.get("error")
        or response.get("body") != []
    ):
        return response

    legacy = client.request(
        "GET",
        "/api/datadirect/ParentStudentUserPerformance/",
        params={
            "userId": _student_id(),
            "personaId": _persona_id(),
            "schoolYearLabel": school_year,
        },
    )
    if (
        not 200 <= legacy.get("status", 0) < 300
        or legacy.get("error")
        or not isinstance(legacy.get("body"), list)
    ):
        return legacy
    return {
        **legacy,
        "body": [
            row for row in legacy["body"] if row.get("performance_type") == "Report"
        ],
        "source": "legacy",
    }


@mcp.tool()
def transcript_templates(school_year: str | None = None) -> dict[str, Any]:
    """List transcript templates for an available school-year label.

    school_year defaults to MSA_SCHOOL_YEAR or the date-derived school year.
    """
    return _get_client().request(
        "GET",
        "/api/Grading/StudentTranscriptTemplateList",
        params={
            "studentId": _student_id(),
            "schoolYearLabel": _school_year() if school_year is None else school_year,
        },
    )


@mcp.tool()
def attendance(school_year: str | None = None) -> dict[str, Any]:
    """Get attendance records for an available school-year label.

    school_year defaults to MSA_SCHOOL_YEAR or the date-derived school year.
    """
    return _get_client().request(
        "GET",
        "/api/datadirect/ParentStudentUserAttendance/",
        params={
            "userId": _student_id(),
            "personaId": _persona_id(),
            "schoolYearLabel": _school_year() if school_year is None else school_year,
        },
    )


@mcp.tool()
def conduct(level_num: int = 0, school_year: str | None = None) -> dict[str, Any]:
    """Get conduct records.

    Args:
        level_num: Conduct level filter (0 = all).
        school_year: Exact label from `school_years()`. None uses
            MSA_SCHOOL_YEAR or the date-derived school year.
    """
    return _get_client().request(
        "GET",
        "/api/datadirect/ParentStudentUserConduct/",
        params={
            "studentUserId": _student_id(),
            "viewerPersonaId": _persona_id(),
            "schoolYearLabel": _school_year() if school_year is None else school_year,
            "levelNum": level_num,
        },
    )


@mcp.tool()
def grade_levels() -> dict[str, Any]:
    """List the student's grade-level history."""
    return _get_client().request("GET", "/api/datadirect/StudentGradeLevelList/")


@mcp.tool()
def school_years() -> dict[str, Any]:
    """List enrolled/available school-year labels from grade_levels().

    Labels retain their exact spelling and source order, with duplicates
    removed. They may include historical, current, and future school years.
    """
    response = grade_levels()
    body = response.get("body")
    if (
        not _response_succeeded(response)
        or not isinstance(body, list)
        or any(
            not isinstance(row, dict) or not isinstance(row.get("SchoolYearLabel"), str)
            for row in body
        )
    ):
        return response
    labels = list(dict.fromkeys(row["SchoolYearLabel"] for row in body))
    return {**response, "body": labels}


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
def group_membership(
    kind: str, duration_id: int = 0, school_year: str | None = None
) -> dict[str, Any]:
    """List the student's group memberships of a given kind.

    Args:
        kind: One of: advisory, athletic, dorm, activity, community.
        duration_id: A DurationId from `student_terms()`. 0 / omitted =
            auto-resolve the current term for this kind of group.
            Community defaults to 0 (school-wide), without a term lookup.
        school_year: Exact label from `school_years()`. None uses the configured
            or date-derived year. If that year has no current term, supply a
            duration_id from `student_terms(school_year=...)`.
    """
    if kind not in _GROUP_ENDPOINTS:
        raise UserError(
            f"Unknown kind '{kind}'. Valid: {', '.join(sorted(_GROUP_ENDPOINTS))}."
        )
    endpoint, dur_param, offering_type = _GROUP_ENDPOINTS[kind]
    if not duration_id and kind != "community":
        duration_id = (
            _resolve_duration_id(offering_type)
            if school_year is None
            else _resolve_duration_id(offering_type, school_year=school_year)
        )
    return _get_client().request(
        "GET",
        f"/api/datadirect/{endpoint}",
        params={
            "userId": _student_id(),
            "schoolYearLabel": _school_year() if school_year is None else school_year,
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
def calendar_events(
    date_start: str,
    date_end: str,
    calendar_ids: list[str] | None = None,
    include_practice: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """Read school, group, and athletic calendar events in a date range.

    This uses the site's read-only events POST; it never creates events or
    saves calendar preferences. Assignments and class/admissions schedules
    are separate: use assignments or schedule for those.

    Args:
        date_start: Exact YYYY-MM-DD school-local start date.
        date_end: Exact YYYY-MM-DD school-local end date; boundaries are
            passed through without an inclusive/exclusive adjustment.
        calendar_ids: Child CalendarId values from calendar_list(). None
            uses currently selected supported filters. [] requests nothing.
            Explicit visible IDs affect this request only, not saved settings.
        include_practice: Include practice events (default False).
        full: Return raw rows without compaction or deduplication. Compact
            output preserves local dates and merges duplicate event groups;
            count and raw_count describe the returned and original row counts.
    """
    from .calendar_tools import fetch_calendar_events

    return fetch_calendar_events(
        _get_client(),
        date_start,
        date_end,
        calendar_ids=calendar_ids,
        include_practice=include_practice,
        full=full,
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
        to_date = _today().isoformat()
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
def directory_list() -> dict[str, Any]:
    """List available directories (DirectoryID, SortOrder, DirectoryName).

    Returns only Directories and response metadata, without session context
    or directory members. Use the IDs with the other directory tools.
    """
    response = _get_client().request("GET", "/api/webapp/context")
    body = response.get("body")
    directories = body.get("Directories") if isinstance(body, dict) else None
    result = {**response, "body": directories}
    if not isinstance(directories, list):
        result["error"] = response.get("error") or "Expected a Directories list."
    return result


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
        directory_id: Numeric directory id from `directory_list()`.
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


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _api_writes_allowed() -> bool:
    return os.environ.get("MSA_ALLOW_WRITES", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


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

    Only GET/HEAD/OPTIONS are allowed unless the server was started with
    MSA_ALLOW_WRITES=true. Every typed tool is read-only; this is the one
    place a write could happen, so it is off by default.

    Args:
        method: HTTP method.
        path: Path starting with `/`.
        params: Query string parameters.
        json_body: JSON body for POST/PUT/PATCH.
        form_data: Form-encoded body.
        extra_headers: Additional headers (e.g. X-CSRF-Token).
    """
    verb = method.strip().upper()
    if verb not in _SAFE_METHODS and not _api_writes_allowed():
        raise UserError(
            f"api_request refuses {verb}: write methods are disabled. Start the "
            "server with MSA_ALLOW_WRITES=true to permit POST/PUT/PATCH/DELETE."
        )
    return _get_client().request(
        verb,
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
