"""MCP server exposing myschoolapp.com via FastMCP.

Endpoints were mapped by capturing live network traffic from a logged-in
student session on Tabor Academy's myschoolapp deployment. They follow the
standard Blackbaud onMSA SPA conventions and should work on any school's
instance, though some IDs (categoryId, durationId, directoryId) are
school-specific.
"""

from __future__ import annotations

import json as _json
import os
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


@mcp.tool()
def gradebook(duration_id: int, section_ids: list[int]) -> dict[str, Any]:
    """Get gradebook summary with marking-period grades for sections.

    Args:
        duration_id: A DurationId from `student_terms()`.
        section_ids: LeadSectionIds from `classes()`.
    """
    duration_section_list = _json.dumps(
        [
            {
                "DurationId": duration_id,
                "LeadSectionList": [{"LeadSectionId": sid} for sid in section_ids],
            }
        ]
    )
    return _get_client().request(
        "GET",
        "/api/gradebook/GradeBookMyDayMarkingPeriods",
        params={
            "durationSectionList": duration_section_list,
            "userId": _student_id(),
            "personaId": _persona_id(),
        },
    )


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
