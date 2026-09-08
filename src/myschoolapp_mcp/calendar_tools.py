"""Read-only calendar event fetching, separate from MCP registration."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .errors import SessionError, UserError
from .formatting import SENTINEL, mdy, strip_html

if TYPE_CHECKING:
    from .client import MyschoolappClient

_EXCLUDED_PRESET_TYPES = {4, 10, 11, 12, 13, 14}
_EVENT_FIELDS = {
    "EventId": "event_id",
    "UserId": "user_id",
    "StartDate": "start_date",
    "Title": "title",
    "AllDay": "all_day",
    "TotalDays": "total_days",
    "BriefDescription": "brief_description",
    "LongDescription": "long_description",
    "EventType": "event_type",
    "Location": "location",
    "BuildingName": "building_name",
    "RoomName": "room_name",
    "Opponent": "opponent",
    "HomeAway": "home_away",
    "Cancelled": "cancelled",
    "Rescheduled": "rescheduled",
    "RescheduleNote": "reschedule_note",
}


def _compact_events(rows: list) -> list[dict]:
    events: dict[tuple, dict] = {}
    for index, row in enumerate(rows):
        message = f"Unexpected calendar event shape at row {index}."
        if not isinstance(row, dict) or not all(
            field in row for field in ("EventId", "UserId", "StartDate", "Title")
        ):
            raise UserError(message)
        key = (row["EventId"], row["UserId"], row["StartDate"])
        if all(value is None for value in key):
            raise UserError(message)
        try:
            hash(key)
        except TypeError:
            raise UserError(message) from None
        if key not in events:
            item = {
                name: (None if row[field] == SENTINEL else row[field])
                for field, name in _EVENT_FIELDS.items()
                if field in row
            }
            for name in ("brief_description", "long_description"):
                if name in item:
                    item[name] = strip_html(item[name])
            item["group_names"] = []
            events[key] = item
        group = row.get("GroupName")
        if group and group not in events[key]["group_names"]:
            events[key]["group_names"].append(group)
    return list(events.values())


def _succeeded(response: dict) -> bool:
    status = response.get("status")
    return isinstance(status, int) and 200 <= status < 300 and not response.get("error")


def _error(response: dict, message: str) -> dict:
    result = dict(response)
    result["error"] = response.get("error") or True
    result.setdefault("message", message)
    return result


def _redact_token(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    if isinstance(value, list):
        return [_redact_token(item, token) for item in value]
    if isinstance(value, dict):
        return {
            _redact_token(key, token): _redact_token(item, token)
            for key, item in value.items()
        }
    return value


def _valid_definitions(body: object) -> bool:
    if not isinstance(body, list):
        return False
    for parent in body:
        if (
            not isinstance(parent, dict)
            or parent.get("Selected") not in (True, False)
            or not isinstance(parent.get("Filters"), list)
        ):
            return False
        for child in parent["Filters"]:
            if (
                not isinstance(child, dict)
                or child.get("Selected") not in (True, False)
                or not isinstance(child.get("CalendarId"), str)
                or not child["CalendarId"]
                or not isinstance(child.get("PresetTypeId"), int)
            ):
                return False
    return True


def _select_calendar_ids(definitions: list, requested: list[str] | None) -> list[str]:
    supported = set()
    selected = []
    for parent in definitions:
        for child in parent["Filters"]:
            if child["PresetTypeId"] in _EXCLUDED_PRESET_TYPES:
                continue
            calendar_id = child["CalendarId"]
            supported.add(calendar_id)
            if parent["Selected"] and child["Selected"]:
                selected.append(calendar_id)
    if requested is not None:
        for calendar_id in requested:
            if calendar_id not in supported:
                raise UserError(
                    f"Unknown or unsupported calendar ID: {calendar_id!r}."
                )
        selected = requested
    return list(dict.fromkeys(selected))


def fetch_calendar_events(
    client: MyschoolappClient,
    date_start: str,
    date_end: str,
    calendar_ids: list[str] | None = None,
    include_practice: bool = False,
    full: bool = False,
) -> dict:
    """Fetch calendar events; assignments and class/admissions schedules are separate.

    Dates must be exact YYYY-MM-DD values; range endpoints are not adjusted.
    Default selection uses selected parents and supported selected child filters.
    Explicit IDs must be visible supported child filters, regardless of selection.
    Empty selections skip the CSRF request and events POST entirely.

    The events POST reads events and does not save calendar preferences.
    Compact rows retain local dates, with unique group_names in source order.
    count describes returned rows; raw_count describes rows before deduplication.
    full=True bypasses compaction and row validation. Responses retain diagnostic
    details, except CSRF responses and any reflected token values are sanitized.
    """
    for value in (date_start, date_end):
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
        ):
            raise UserError("Invalid date: expected YYYY-MM-DD.")
    start, end = mdy(date_start), mdy(date_end)
    if date_start > date_end:
        raise UserError("date_start must be on or before date_end.")
    definitions = client.request(
        "GET",
        "/api/mycalendar/list/",
        params={
            "startDate": start,
            "endDate": end,
            "settingsTypeId": 1,
            "calendarSetId": 1,
            "recentFilterSave": "false",
        },
    )
    if not _succeeded(definitions):
        return _error(definitions, "Calendar definitions request failed.")
    if not _valid_definitions(definitions.get("body")):
        return _error(definitions, "Unexpected calendar definitions shape.")
    selected = _select_calendar_ids(definitions["body"], calendar_ids)
    if not selected:
        return {
            "status": 200,
            "body": [],
            "count": 0,
            "raw_count": 0,
            "note": "No supported calendars selected; events were not requested.",
        }
    try:
        csrf = client.request("GET", "/api/security/csrftoken")
    except RuntimeError:
        raise SessionError("CSRF token request failed.") from None
    token = csrf.get("body")
    if (
        not _succeeded(csrf)
        or not isinstance(token, str)
        or not token.strip()
        or "\r" in token
        or "\n" in token
    ):
        return {
            "status": csrf.get("status")
            if isinstance(csrf.get("status"), int)
            else None,
            "error": True,
            "message": "CSRF token request failed or returned an unexpected shape.",
        }
    try:
        response = client.request(
            "POST",
            "/api/mycalendar/events",
            json_body={
                "bulkURL": "mycalendar/events",
                "startDate": start,
                "endDate": end,
                "filterString": ",".join(selected),
                "showPractice": include_practice,
                "recentSave": False,
            },
            extra_headers={"RequestVerificationToken": token},
        )
    except RuntimeError as exc:
        # The transport's exception chain may contain request header values.
        raise SessionError(_redact_token(str(exc), token)) from None
    response = _redact_token(response, token)
    if not _succeeded(response):
        return _error(response, "Calendar events request failed.")
    if not isinstance(response.get("body"), list):
        return _error(response, "Unexpected calendar events shape: expected a list.")
    rows = response["body"]
    try:
        body = rows if full else _compact_events(rows)
    except ValueError as exc:
        return {**_error(response, str(exc)), "raw_count": len(rows)}
    return {**response, "body": body, "count": len(body), "raw_count": len(rows)}
