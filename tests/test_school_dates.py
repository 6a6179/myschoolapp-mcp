"""School-local date defaults, using synthetic clocks and no credentials."""

from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


@pytest.fixture
def school_clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc)
            return instant.astimezone(tz)

    monkeypatch.setattr(server, "datetime", FrozenDatetime, raising=False)
    monkeypatch.setenv("MSA_TIMEZONE", "America/New_York")
    monkeypatch.setenv("MSA_STUDENT_ID", "synthetic-student")
    monkeypatch.setenv("MSA_PERSONA_ID", "2")
    client = Mock(subdomain="synthetic-school")
    client.request.return_value = {"status": 200, "body": []}
    monkeypatch.setattr(server, "_get_client", lambda: client)
    return client


@pytest.mark.parametrize(
    ("timezone_name", "expected"),
    [("America/New_York", date(2026, 6, 30)), (None, date(2026, 7, 1))],
)
def test_today_uses_configured_timezone_or_utc(
    school_clock, monkeypatch, timezone_name, expected
):
    if timezone_name is None:
        monkeypatch.delenv("MSA_TIMEZONE")
    assert server._today() == expected


@pytest.mark.parametrize("invalid", ["Not/A_Timezone", "", "/UTC"])
def test_invalid_timezone_has_actionable_error(school_clock, monkeypatch, invalid):
    monkeypatch.setenv("MSA_TIMEZONE", invalid)
    with pytest.raises(ValueError, match=r"MSA_TIMEZONE.*IANA"):
        server._today()


def test_assignments_bucket_and_fetch_with_school_local_today(school_clock):
    school_clock.request.side_effect = [
        {
            "status": 200,
            "body": [
                {
                    "assignment_index_id": 12,
                    "short_description": "Synthetic assignment",
                    "date_due": "6/30/2026 11:59 PM",
                }
            ],
        },
        {"status": 200, "body": []},
    ]

    result = server.assignments()

    assert result["counts"]["DueToday"] == 1
    assert result["buckets"]["DueToday"][0]["assignment_index_id"] == 12
    range_params = school_clock.request.call_args_list[0].kwargs["params"]
    assert range_params["dateStart"] == "6/9/2026"
    assert range_params["dateEnd"] == "8/29/2026"
    assert school_clock.request.call_args.kwargs["params"]["dateEnd"] == "6/30/2026"


def test_school_year_inference_uses_school_local_july_boundary(
    school_clock, monkeypatch
):
    monkeypatch.delenv("MSA_SCHOOL_YEAR", raising=False)
    assert server._school_year() == "2025 - 2026"
    monkeypatch.delenv("MSA_TIMEZONE")
    assert server._school_year() == "2026 - 2027"
    monkeypatch.setenv("MSA_SCHOOL_YEAR", "Exact configured year")
    assert server._school_year() == "Exact configured year"


@pytest.mark.parametrize("on_date", [None, "", "2025-02-03"])
def test_schedule_sends_explicit_school_date(school_clock, on_date):
    server.schedule(on_date=on_date)
    expected = "2/3/2025" if on_date else "6/30/2026"
    assert school_clock.request.call_args.kwargs["params"]["scheduleDate"] == expected


@pytest.mark.parametrize("on_date", [None, "", "2025-02-03"])
def test_daily_announcement_sends_explicit_school_date(school_clock, on_date):
    server.daily_announcement(on_date=on_date)
    expected = "2/3/2025" if on_date else "6/30/2026"
    assert school_clock.request.call_args.kwargs["params"]["mydayDate"] == expected


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        (None, None, "6/30/2026", "7/30/2026"),
        ("2025-02-03", None, "2/3/2025", "7/30/2026"),
        (None, "2025-02-03", "6/30/2026", "2/3/2025"),
        ("2025-02-03", "2025-02-04", "2/3/2025", "2/4/2025"),
    ],
)
def test_assignment_range_school_date_defaults(
    school_clock, start, end, expected_start, expected_end
):
    server.assignments_in_range(date_start=start, date_end=end)
    params = school_clock.request.call_args.kwargs["params"]
    assert params["dateStart"] == expected_start
    assert params["dateEnd"] == expected_end


@pytest.mark.parametrize("to_date", [None, "", "2025-02-03"])
def test_official_notes_school_date_default(school_clock, to_date):
    server.official_notes(to_date=to_date)
    expected = "2/3/2025" if to_date else "6/30/2026"
    assert school_clock.request.call_args.kwargs["params"]["toDate"] == expected


@pytest.mark.parametrize("timezone_name", ["America/New_York", None])
def test_config_includes_resolved_timezone(school_clock, monkeypatch, timezone_name):
    if timezone_name is None:
        monkeypatch.delenv("MSA_TIMEZONE")
    assert server.config()["timezone"] == (timezone_name or "UTC")
