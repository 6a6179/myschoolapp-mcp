"""Regressions for the post-review fixes: auth-expiry detection, opt-in
auto-refresh, api_request write gating, title unescaping, calendar
sentinels, and the assignments forward window."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from myschoolapp_mcp import calendar_tools, server
from myschoolapp_mcp.client import MyschoolappClient, is_auth_expired
from myschoolapp_mcp.formatting import compact_assignment

AUTH_403 = {
    "Error": "Request authorization failed.",
    "ErrorType": "INVALID_AUTHORIZATION",
    "ErrorId": 9,
}


# ---------------------------------------------------------------------------
# client: expiry detection + auto refresh
# ---------------------------------------------------------------------------


def _client_with(handler, monkeypatch):
    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    return MyschoolappClient(subdomain="testschool", cookies={"t": "old"})


def test_is_auth_expired_recognises_blackbaud_json_403():
    assert is_auth_expired({"status": 403, "body": AUTH_403})
    assert is_auth_expired({"status": 401, "body": AUTH_403})
    assert not is_auth_expired({"status": 403, "body": {"ErrorType": "OTHER"}})
    assert not is_auth_expired({"status": 200, "body": AUTH_403})
    assert not is_auth_expired({"status": 403, "body": "text"})


def test_json_403_gets_hint_and_flag_without_hook(monkeypatch):
    def handler(request):
        return httpx.Response(403, json=AUTH_403)

    c = _client_with(handler, monkeypatch)
    out = c.request("GET", "/api/x")
    assert out["status"] == 403
    assert out["error"] is True
    assert out["auth_expired"] is True
    assert "cookie_refresh" in out["hint"]
    assert "auto_refreshed" not in out


def test_html_login_page_still_flags_expiry(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            text="<!DOCTYPE html><html>login</html>",
            headers={"content-type": "text/html"},
        )

    c = _client_with(handler, monkeypatch)
    out = c.request("GET", "/api/x")
    assert out["auth_expired"] is True
    assert "HTML" in out["hint"]


def test_auto_refresh_retries_once_with_new_cookies(monkeypatch):
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("cookie", ""))
        if "t=new" in seen[-1]:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403, json=AUTH_403)

    c = _client_with(handler, monkeypatch)
    hook = MagicMock(return_value={"t": "new"})
    c.refresh_hook = hook

    out = c.request("GET", "/api/x")
    assert out["status"] == 200
    assert out["auto_refreshed"] is True
    assert hook.call_count == 1
    assert len(seen) == 2 and "t=old" in seen[0] and "t=new" in seen[1]


def test_auto_refresh_respects_cooldown(monkeypatch):
    def handler(request):
        return httpx.Response(403, json=AUTH_403)

    c = _client_with(handler, monkeypatch)
    hook = MagicMock(return_value={"t": "new"})
    c.refresh_hook = hook

    first = c.request("GET", "/api/x")
    second = c.request("GET", "/api/x")
    assert hook.call_count == 1  # second call is inside the cooldown
    assert "still unauthorized" in first["auto_refresh_error"]
    assert second["auth_expired"] is True
    assert "auto_refresh_error" not in second


def test_auto_refresh_failure_is_reported_not_raised(monkeypatch):
    def handler(request):
        return httpx.Response(403, json=AUTH_403)

    c = _client_with(handler, monkeypatch)
    c.refresh_hook = MagicMock(side_effect=RuntimeError("login broke"))
    out = c.request("GET", "/api/x")
    assert out["auth_expired"] is True
    assert "login broke" in out["auto_refresh_error"]


def test_replace_cookies_keeps_host_scope_and_secure():
    c = MyschoolappClient(subdomain="testschool", cookies={"t": "old"})
    try:
        c.replace_cookies({"t": "new", "s": "x"})
        jar = list(c._client.cookies.jar)
        assert {ck.name: ck.value for ck in jar} == {"t": "new", "s": "x"}
        assert all(ck.domain == "testschool.myschoolapp.com" for ck in jar)
        assert all(ck.secure for ck in jar)
    finally:
        c.close()


# ---------------------------------------------------------------------------
# server: hook install is opt-in
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_client(monkeypatch):
    monkeypatch.setenv("MSA_SUBDOMAIN", "testschool")
    monkeypatch.setenv("MSA_COOKIE", "t=synthetic")
    monkeypatch.delenv("MSA_AUTO_REFRESH", raising=False)
    monkeypatch.delenv("MSA_ALLOW_WRITES", raising=False)
    server._drop_client()
    yield
    server._drop_client()


def test_hook_not_installed_by_default(fresh_client):
    assert server._get_client().refresh_hook is None
    assert server.config()["auto_refresh"] is False


def test_hook_requires_credentials(fresh_client, monkeypatch):
    monkeypatch.setenv("MSA_AUTO_REFRESH", "true")
    monkeypatch.delenv("SCHOOL_EMAIL", raising=False)
    monkeypatch.delenv("SCHOOL_PASS", raising=False)
    assert server._get_client().refresh_hook is None


def test_hook_installed_when_opted_in(fresh_client, monkeypatch):
    monkeypatch.setenv("MSA_AUTO_REFRESH", "1")
    monkeypatch.setenv("SCHOOL_EMAIL", "a@b")
    monkeypatch.setenv("SCHOOL_PASS", "x")
    assert server._get_client().refresh_hook is not None
    assert server.config()["auto_refresh"] is True


# ---------------------------------------------------------------------------
# server: api_request write gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "put", "Patch", "DELETE"])
def test_api_request_blocks_writes_by_default(fresh_client, monkeypatch, method):
    fake = MagicMock()
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    with pytest.raises(ValueError, match="write methods are disabled"):
        server.api_request(method, "/api/x")
    fake.request.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "get", "HEAD", "OPTIONS"])
def test_api_request_allows_reads(fresh_client, monkeypatch, method):
    fake = MagicMock()
    fake.request.return_value = {"status": 200, "body": []}
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    server.api_request(method, "/api/x")
    assert fake.request.call_args.args[0] == method.upper()


def test_api_request_allows_writes_when_enabled(fresh_client, monkeypatch):
    monkeypatch.setenv("MSA_ALLOW_WRITES", "true")
    fake = MagicMock()
    fake.request.return_value = {"status": 200, "body": {}}
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    server.api_request("POST", "/api/x", json_body={"a": 1})
    assert fake.request.call_args.args[0] == "POST"
    assert server.config()["api_request_writes"] is True


# ---------------------------------------------------------------------------
# formatting / calendar
# ---------------------------------------------------------------------------


def test_compact_assignment_unescapes_title():
    item = {
        "short_description": "Finish Unit 1 Vocabulary Exercise&#160;",
        "date_due": "9/9/2026 10:20 AM",
    }
    assert compact_assignment(item)["title"] == "Finish Unit 1 Vocabulary Exercise"


def test_compact_assignment_title_amp_and_whitespace():
    item = {"short_description": "Q&amp;A   review\u00a0", "date_due": "9/9/2026"}
    assert compact_assignment(item)["title"] == "Q&A review"


def test_calendar_compaction_nulls_sentinel():
    rows = [
        {
            "EventId": 1,
            "UserId": 2,
            "StartDate": "9/9/2026 2:00 PM",
            "Title": "Game",
            "TotalDays": -2147483648,
            "GroupName": "Soccer",
        }
    ]
    out = calendar_tools._compact_events(rows)
    assert out[0]["total_days"] is None
    assert out[0]["title"] == "Game"


# ---------------------------------------------------------------------------
# assignments: days_ahead
# ---------------------------------------------------------------------------


def _assignments_client(monkeypatch):
    fake = MagicMock()
    fake.request.return_value = {"status": 200, "url": "u", "body": []}
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    return fake


def test_assignments_days_ahead_changes_window(monkeypatch):
    fake = _assignments_client(monkeypatch)
    out = server.assignments(days_ahead=120)
    range_call = fake.request.call_args_list[0]
    from datetime import date, timedelta

    today = server._today()
    end = today + timedelta(days=120)
    assert range_call.kwargs["params"]["dateEnd"] == f"{end.month}/{end.day}/{end.year}"
    assert out["window"]["end"] == end.isoformat()
    assert out["window"]["start"] == (today - timedelta(days=21)).isoformat()
    assert isinstance(date.today(), date)


@pytest.mark.parametrize("bad", [0, -5, 366])
def test_assignments_days_ahead_validated(monkeypatch, bad):
    _assignments_client(monkeypatch)
    with pytest.raises(ValueError, match="days_ahead"):
        server.assignments(days_ahead=bad)


def test_gradebook_partial_marks_status_synthetic(monkeypatch):
    fake = MagicMock()
    fake.request.side_effect = [
        {
            "status": 200,
            "body": [
                {"leadsectionid": 11, "markingperiodid": 5, "sectionidentifier": "A"},
                {"leadsectionid": 22, "markingperiodid": 5, "sectionidentifier": "B"},
            ],
        },
        {"status": 403, "error": True, "body": {}},
        {
            "status": 200,
            "body": {
                "Roster": [
                    {"StudentUserId": "1", "SectionGrade": 90, "SectionGradeYear": 0}
                ]
            },
        },
    ]
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    monkeypatch.setenv("MSA_STUDENT_ID", "1")
    out = server.gradebook(duration_id=7)
    assert out["status"] == 207
    assert out["status_source"] == "synthetic"
    assert out["partial"] is True
