"""Offline calendar helper tests using only synthetic responses."""

from copy import deepcopy
from traceback import format_exception

import pytest

from myschoolapp_mcp.calendar_tools import fetch_calendar_events

START = "2026-02-03"
END = "2026-02-10"
TOKEN = "synthetic-csrf-token"


class FakeClient:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def calendar_filter(calendar_id="metafilter_1", *, selected=True, preset=0):
    return {
        "Selected": selected,
        "CalendarId": calendar_id,
        "FilterName": "Synthetic filter",
        "PresetTypeId": preset,
    }


def calendar_parent(*filters, selected=True):
    return {
        "Selected": selected,
        "IncludePractice": True,
        "CalendarId": "synthetic-parent",
        "Calendar": "Synthetic calendar",
        "PresetTypeId": 0,
        "Filters": list(filters),
    }


def definitions():
    return [calendar_parent(calendar_filter())]


def event(**changes):
    return {
        "EventId": "synthetic-event",
        "UserId": "synthetic-user",
        "StartDate": "2/3/2026 3:00 PM",
        "Title": "Synthetic gathering",
        "GroupName": "Synthetic club",
        **changes,
    }


def event_client(rows, *, calendars=None):
    return FakeClient(
        {"status": 200, "body": definitions() if calendars is None else calendars},
        {"status": 200, "body": TOKEN},
        {"status": 200, "url": "/api/mycalendar/events", "body": rows},
    )


@pytest.mark.parametrize(
    "options,practice", [({}, False), ({"include_practice": True}, True)]
)
def test_full_request_contract(options, practice):
    rows = [event(), event()]
    original = deepcopy(rows)
    client = event_client(rows)

    result = fetch_calendar_events(client, START, END, full=True, **options)

    assert result == {
        "status": 200,
        "url": "/api/mycalendar/events",
        "body": rows,
        "count": 2,
        "raw_count": 2,
    }
    assert rows == original
    assert TOKEN not in repr(result)
    assert client.calls == [
        (
            "GET",
            "/api/mycalendar/list/",
            {
                "params": {
                    "startDate": "2/3/2026",
                    "endDate": "2/10/2026",
                    "settingsTypeId": 1,
                    "calendarSetId": 1,
                    "recentFilterSave": "false",
                }
            },
        ),
        ("GET", "/api/security/csrftoken", {}),
        (
            "POST",
            "/api/mycalendar/events",
            {
                "json_body": {
                    "bulkURL": "mycalendar/events",
                    "startDate": "2/3/2026",
                    "endDate": "2/10/2026",
                    "filterString": "metafilter_1",
                    "showPractice": practice,
                    "recentSave": False,
                },
                "extra_headers": {"RequestVerificationToken": TOKEN},
            },
        ),
    ]


@pytest.mark.parametrize("field", ["date_start", "date_end"])
@pytest.mark.parametrize(
    "invalid",
    [
        "",
        None,
        20260203,
        "20260203",
        "2026-W06-2",
        "2026-2-03",
        "2026-02-3",
        " 2026-02-03",
        "2026-02-03 ",
        "2026-02-03\n",
        "2026-02-30",
        "2026-13-01",
        "0000-01-01",
        "2026-02-03T00:00:00",
    ],
)
def test_invalid_dates_fail_before_requests(field, invalid):
    client = FakeClient()
    dates = {"date_start": START, "date_end": END, field: invalid}

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        fetch_calendar_events(client, **dates)

    assert client.calls == []


def test_reversed_range_fails_before_requests():
    client = FakeClient()
    with pytest.raises(ValueError, match=r"date_start.*date_end"):
        fetch_calendar_events(client, END, START)
    assert client.calls == []


@pytest.mark.parametrize(
    "start,end,expected_start,expected_end",
    [
        (START, START, "2/3/2026", "2/3/2026"),
        ("2024-02-29", "2030-12-31", "2/29/2024", "12/31/2030"),
    ],
)
def test_range_is_not_adjusted_or_capped(start, end, expected_start, expected_end):
    client = event_client([])

    fetch_calendar_events(client, start, end, full=True)

    assert client.calls[0][2]["params"]["startDate"] == expected_start
    assert client.calls[0][2]["params"]["endDate"] == expected_end
    assert client.calls[2][2]["json_body"]["startDate"] == expected_start
    assert client.calls[2][2]["json_body"]["endDate"] == expected_end


def test_default_selection_gates_parents_children_and_excludes_unsupported():
    calendars = [
        calendar_parent(
            calendar_filter("synthetic-events", preset=2),
            calendar_filter("metafilter_1"),
            calendar_filter("synthetic-unselected", selected=False),
            *(
                calendar_filter(f"synthetic-excluded-{kind}", preset=kind)
                for kind in (4, 10, 11, 12, 13, 14)
            ),
            calendar_filter("synthetic-events", preset=2),
        ),
        calendar_parent(calendar_filter("synthetic-hidden"), selected=False),
    ]
    original = deepcopy(calendars)
    client = event_client([], calendars=calendars)

    fetch_calendar_events(client, START, END)

    assert client.calls[2][2]["json_body"]["filterString"] == (
        "synthetic-events,metafilter_1"
    )
    assert calendars == original


def test_explicit_ids_use_requested_order_without_saved_selection():
    calendars = [
        calendar_parent(calendar_filter()),
        calendar_parent(
            calendar_filter("synthetic-opt-in", selected=False), selected=False
        ),
    ]
    requested = ["synthetic-opt-in", "metafilter_1", "synthetic-opt-in"]
    original = deepcopy((calendars, requested))
    client = event_client([], calendars=calendars)

    fetch_calendar_events(client, START, END, calendar_ids=requested)

    assert client.calls[2][2]["json_body"]["filterString"] == (
        "synthetic-opt-in,metafilter_1"
    )
    assert (calendars, requested) == original


@pytest.mark.parametrize(
    "calendar_id",
    [
        "synthetic-unknown",
        "synthetic-parent",
        " metafilter_1",
        "",
        *(f"synthetic-excluded-{kind}" for kind in (4, 10, 11, 12, 13, 14)),
    ],
)
def test_unknown_or_unsupported_ids_fail_without_csrf_or_post(calendar_id):
    calendars = [
        calendar_parent(
            calendar_filter(),
            *(
                calendar_filter(f"synthetic-excluded-{kind}", preset=kind)
                for kind in (4, 10, 11, 12, 13, 14)
            ),
        )
    ]
    client = FakeClient({"status": 200, "body": calendars})

    with pytest.raises(ValueError, match="Unknown or unsupported calendar ID"):
        fetch_calendar_events(client, START, END, calendar_ids=[calendar_id])

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "calendars,requested",
    [
        (definitions(), []),
        ([], None),
        ([calendar_parent(calendar_filter(), selected=False)], None),
        ([calendar_parent(calendar_filter(selected=False))], None),
        ([calendar_parent(calendar_filter(preset=4))], None),
        ([calendar_parent()], None),
    ],
)
@pytest.mark.parametrize("full", [False, True])
def test_empty_selection_never_requests_events(calendars, requested, full):
    client = FakeClient({"status": 200, "body": calendars})

    result = fetch_calendar_events(
        client, START, END, calendar_ids=requested, full=full
    )

    assert result["status"] == 200
    assert result["body"] == []
    assert result["count"] == result["raw_count"] == 0
    assert "selected" in result["note"]
    assert not result.get("error")
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"status": 403, "error": True, "body": {"message": "Synthetic denial"}},
        {"status": 503, "body": []},
        {"status": 302, "body": []},
        {
            "status": 200,
            "error": True,
            "body": "<html>Synthetic login</html>",
            "hint": "Synthetic session expired",
        },
        {"status": 200, "error": "synthetic-error", "body": []},
        {"status": 200, "body": {}},
        {"status": 200, "body": None},
        {"status": 200, "body": ""},
        {"status": 200},
        {"body": []},
    ],
)
def test_definitions_failures_preserve_response_and_stop(response):
    response = {"url": "/api/mycalendar/list/", **response}
    original = deepcopy(response)
    client = FakeClient(response)

    result = fetch_calendar_events(client, START, END)

    assert result.get("error")
    for key, value in original.items():
        assert result[key] == value
    assert "count" not in result
    assert response == original
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        [None],
        ["unexpected"],
        [{}],
        [{"Selected": True}],
        [{"Selected": True, "Filters": None}],
        [{"Selected": True, "Filters": {}}],
        [{"Filters": []}],
        [calendar_parent(None)],
        [calendar_parent({})],
        [calendar_parent({"Selected": True, "PresetTypeId": 0})],
        [calendar_parent({"CalendarId": "metafilter_1", "PresetTypeId": 0})],
        [calendar_parent({"Selected": True, "CalendarId": "metafilter_1"})],
        [calendar_parent(calendar_filter(None))],
        [calendar_parent(calendar_filter(""))],
        [calendar_parent(calendar_filter([]))],
        [calendar_parent(calendar_filter(preset=[]))],
        [calendar_parent(calendar_filter(preset=None))],
        [calendar_parent(calendar_filter(selected="false"))],
        [calendar_parent(selected="false")],
        [calendar_parent(None, selected=False)],
    ],
)
def test_malformed_definitions_return_explicit_shape_error(body):
    response = {"status": 200, "body": body}
    original = deepcopy(response)
    client = FakeClient(response)

    result = fetch_calendar_events(client, START, END, calendar_ids=[])

    assert result["error"] is True
    assert result["status"] == 200
    assert result["body"] == body
    assert "Unexpected calendar definitions shape" in result["message"]
    assert response == original
    assert len(client.calls) == 1


def test_definitions_network_failure_surfaces_unchanged():
    failure = RuntimeError("Synthetic definitions connection failure")
    client = FakeClient(failure)
    with pytest.raises(RuntimeError) as caught:
        fetch_calendar_events(client, START, END)
    assert caught.value is failure
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "csrf",
    [
        {"status": 403, "error": True, "body": TOKEN},
        {"status": 503, "body": {"detail": TOKEN}},
        {"status": 302, "body": TOKEN},
        {"status": 200, "error": TOKEN, "body": TOKEN},
        {"status": 200, "error": True, "body": f"<html>{TOKEN}</html>"},
        {"status": 200, "body": None},
        {"status": 200, "body": {"token": TOKEN}},
        {"status": 200, "body": [TOKEN]},
        {"status": 200, "body": 123},
        {"status": 200, "body": ""},
        {"status": 200, "body": "   "},
        {"status": 200, "body": f"{TOKEN}\r\nSynthetic: header"},
        {"status": 200},
        {"body": TOKEN},
        {"status": TOKEN, "body": TOKEN},
    ],
)
def test_csrf_failures_are_sanitized_and_stop(csrf, capsys, caplog):
    csrf = {
        "url": f"/api/security/csrftoken?synthetic={TOKEN}",
        "message": TOKEN,
        "hint": TOKEN,
        **csrf,
    }
    original = deepcopy(csrf)
    client = FakeClient({"status": 200, "body": definitions()}, csrf)

    result = fetch_calendar_events(client, START, END)

    assert result["status"] == (
        csrf["status"] if isinstance(csrf.get("status"), int) else None
    )
    assert result["error"] is True
    assert "CSRF" in result["message"]
    assert set(result) == {"status", "error", "message"}
    assert TOKEN not in repr(result)
    assert TOKEN not in capsys.readouterr().out + caplog.text
    assert csrf == original
    assert len(client.calls) == 2


def test_csrf_network_failure_does_not_expose_exception_text(capsys, caplog):
    client = FakeClient(
        {"status": 200, "body": definitions()},
        RuntimeError(f"Synthetic connection failure: {TOKEN}"),
    )

    with pytest.raises(RuntimeError, match="CSRF") as caught:
        fetch_calendar_events(client, START, END)

    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__
    assert TOKEN not in capsys.readouterr().out + caplog.text
    assert len(client.calls) == 2


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        {"status": 403, "error": True, "body": {"detail": "Synthetic denial"}},
        {"status": 503, "body": [event()]},
        {"status": 302, "body": []},
        {"status": 200, "error": "synthetic-error", "body": [event()]},
        {
            "status": 200,
            "error": True,
            "body": "<html>Synthetic login</html>",
            "hint": "Synthetic session expired",
        },
        {"status": 200, "body": {}},
        {"status": 200, "body": None},
        {"status": 200, "body": ""},
        {"status": 200},
        {"body": []},
    ],
)
def test_event_failures_preserve_response(response, full):
    response = {"url": "/api/mycalendar/events", **response}
    original = deepcopy(response)
    client = FakeClient(
        {"status": 200, "body": definitions()},
        {"status": 200, "body": TOKEN},
        response,
    )

    result = fetch_calendar_events(client, START, END, full=full)

    assert result.get("error")
    for key, value in original.items():
        assert result[key] == value
    assert "count" not in result
    assert response == original
    assert len(client.calls) == 3


def test_event_network_failure_preserves_message():
    failure = RuntimeError("Synthetic events connection failure")
    client = FakeClient(
        {"status": 200, "body": definitions()},
        {"status": 200, "body": TOKEN},
        failure,
    )

    with pytest.raises(RuntimeError) as caught:
        fetch_calendar_events(client, START, END)

    assert str(caught.value) == str(failure)
    assert len(client.calls) == 3


@pytest.mark.parametrize("status", [200, 403])
def test_reflected_token_is_redacted_from_event_response(status, capsys, caplog):
    response = {
        "status": status,
        "url": "/api/mycalendar/events",
        "body": [event(Extra={TOKEN: [f"Synthetic reflection: {TOKEN}"]})],
        "hint": f"Synthetic header: {TOKEN}",
    }
    original = deepcopy(response)
    client = FakeClient(
        {"status": 200, "body": definitions()},
        {"status": 200, "body": TOKEN},
        response,
    )

    result = fetch_calendar_events(client, START, END, full=True)

    assert TOKEN not in repr(result)
    assert result["status"] == status
    assert result["body"][0]["Title"] == "Synthetic gathering"
    assert result["body"][0]["Extra"] == {
        "[REDACTED]": ["Synthetic reflection: [REDACTED]"]
    }
    assert TOKEN not in capsys.readouterr().out + caplog.text
    assert response == original


def test_reflected_token_is_redacted_from_event_exception():
    client = FakeClient(
        {"status": 200, "body": definitions()},
        {"status": 200, "body": TOKEN},
        RuntimeError(f"Synthetic transport failure: {TOKEN}"),
    )

    with pytest.raises(RuntimeError, match="Synthetic transport failure") as caught:
        fetch_calendar_events(client, START, END)

    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("chain_attribute", ["__cause__", "__context__"])
def test_event_exception_chain_cannot_disclose_token(chain_attribute):
    failure = RuntimeError("Synthetic events connection failure")
    setattr(failure, chain_attribute, ValueError(TOKEN))
    client = FakeClient(
        {"status": 200, "body": definitions()},
        {"status": 200, "body": TOKEN},
        failure,
    )

    with pytest.raises(RuntimeError) as caught:
        fetch_calendar_events(client, START, END)

    assert str(caught.value) == str(failure)
    assert TOKEN not in "".join(format_exception(caught.value))


def test_compact_fields_use_live_names_and_keep_local_dates():
    row = event(
        AllDay=False,
        TotalDays=0,
        BriefDescription="<p>Synthetic &amp; brief</p>",
        LongDescription="<div>First<br>Second</div>",
        EventType="Synthetic event type",
        Location="Synthetic location",
        BuildingName="Synthetic building",
        RoomName="Synthetic room",
        Opponent="Synthetic opponent",
        HomeAway="Away",
        Cancelled=False,
        Rescheduled=True,
        RescheduleNote="Synthetic note",
        UnknownField="Omitted in compact mode",
    )
    original = deepcopy(row)

    result = fetch_calendar_events(event_client([row]), START, END)

    assert result == {
        "status": 200,
        "url": "/api/mycalendar/events",
        "count": 1,
        "raw_count": 1,
        "body": [
            {
                "event_id": "synthetic-event",
                "user_id": "synthetic-user",
                "start_date": "2/3/2026 3:00 PM",
                "title": "Synthetic gathering",
                "group_names": ["Synthetic club"],
                "all_day": False,
                "total_days": 0,
                "brief_description": "Synthetic & brief",
                "long_description": "First\nSecond",
                "event_type": "Synthetic event type",
                "location": "Synthetic location",
                "building_name": "Synthetic building",
                "room_name": "Synthetic room",
                "opponent": "Synthetic opponent",
                "home_away": "Away",
                "cancelled": False,
                "rescheduled": True,
                "reschedule_note": "Synthetic note",
            }
        ],
    }
    assert row == original


def test_compact_dedupe_merges_unique_groups_in_source_order():
    rows = [
        event(GroupName="Synthetic B"),
        event(GroupName="Synthetic A", Title="Duplicate title"),
        event(UserId="synthetic-other-user", GroupName="Synthetic other"),
        event(StartDate="2/4/2026 3:00 PM"),
        event(GroupName="Synthetic B"),
        event(GroupName=None),
        event(GroupName=""),
    ]
    original = deepcopy(rows)

    result = fetch_calendar_events(event_client(rows), START, END)

    assert result["count"] == 3
    assert result["raw_count"] == 7
    assert result["body"][0]["group_names"] == ["Synthetic B", "Synthetic A"]
    assert result["body"][0]["title"] == "Synthetic gathering"
    assert result["body"][1]["user_id"] == "synthetic-other-user"
    assert result["body"][2]["start_date"] == "2/4/2026 3:00 PM"
    assert rows == original


def test_full_bypasses_dedupe_and_preserves_html_and_unknown_fields():
    rows = [
        event(BriefDescription="<b>Synthetic</b>", UnknownField={"synthetic": 1}),
        event(GroupName="Synthetic other"),
    ]
    original = deepcopy(rows)

    result = fetch_calendar_events(event_client(rows), START, END, full=True)

    assert result["body"] == original
    assert result["count"] == result["raw_count"] == 2
    assert rows == original


def test_empty_events_are_a_success_with_accurate_counts():
    result = fetch_calendar_events(event_client([]), START, END)
    assert result == {
        "status": 200,
        "url": "/api/mycalendar/events",
        "body": [],
        "count": 0,
        "raw_count": 0,
    }


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        "unexpected row",
        [],
        {},
        *(
            {key: value for key, value in event().items() if key != missing}
            for missing in ("EventId", "UserId", "StartDate", "Title")
        ),
        event(EventId=None, UserId=None, StartDate=None),
        event(EventId=[]),
        event(UserId={}),
    ],
)
def test_malformed_event_rows_return_raw_shape_error(malformed):
    rows = [event(), malformed, malformed]
    original = deepcopy(rows)

    result = fetch_calendar_events(event_client(rows), START, END)

    assert result["status"] == 200
    assert result["error"] is True
    assert "Unexpected calendar event shape at row 1" in result["message"]
    assert result["body"] == original
    assert result["raw_count"] == 3
    assert "count" not in result
    assert rows == original


def test_full_bypasses_event_row_validation():
    rows = [None, {}, "unexpected row", event(EventId=[])]

    result = fetch_calendar_events(event_client(rows), START, END, full=True)

    assert result["body"] == rows
    assert result["count"] == result["raw_count"] == 4
    assert not result.get("error")


@pytest.mark.parametrize(
    "changes",
    [
        {"EventId": 0, "UserId": 0},
        {"EventId": 101, "UserId": None},
        {"EventId": "synthetic-text-id", "UserId": "synthetic-text-user"},
        {"Title": None},
    ],
)
def test_required_event_fields_do_not_impose_unverified_scalar_types(changes):
    row = event(**changes)
    row.pop("GroupName")

    result = fetch_calendar_events(event_client([row, row]), START, END)

    assert result["count"] == 1
    assert result["raw_count"] == 2
    assert result["body"] == [
        {
            "event_id": row["EventId"],
            "user_id": row["UserId"],
            "start_date": row["StartDate"],
            "title": row["Title"],
            "group_names": [],
        }
    ]
