"""Unit tests for cookie parsing and URL gating. No network needed."""

import httpx
import pytest

from myschoolapp_mcp.client import (
    MyschoolappClient,
    _load_cookies,
    _load_cookies_from_file,
    _parse_cookie_header,
    resolve_request_path,
)

HOST = "testschool.myschoolapp.com"


@pytest.fixture
def mock_transport_client(monkeypatch):
    """Exercise the real HTTPX redirect loop with an entirely offline transport."""
    original_client = httpx.Client
    clients = []

    def make(handler):
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kwargs: original_client(
                transport=httpx.MockTransport(handler), trust_env=False, **kwargs
            ),
        )
        client = MyschoolappClient(subdomain="testschool", cookies={"t": "synthetic"})
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.mark.parametrize(
    "destination",
    [
        f"http://{HOST}/steal",
        "https://other-school.myschoolapp.com/steal",
        f"https://child.{HOST}/steal",
        f"https://{HOST}.example.com/steal",
    ],
)
def test_redirect_validated_before_every_send(mock_transport_client, destination):
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(302, headers={"Location": destination})
        return httpx.Response(200, json={"unexpected": "unsafe request sent"})

    client = mock_transport_client(handler)
    with pytest.raises(ValueError, match="Refusing to send"):
        client.request("GET", "/start")

    assert [str(request.url) for request in sent] == [
        f"https://{HOST}/start",
        f"https://{HOST}/next",
    ]
    assert all(request.headers["cookie"] == "t=synthetic" for request in sent)


@pytest.mark.parametrize(
    ("status", "content_type", "body", "expected_body", "error"),
    [
        (200, "application/json", '{"ok": true}', {"ok": True}, False),
        (403, "application/json", '{"denied": true}', {"denied": True}, True),
        (200, "text/html", "<html>Sign in</html>", "<html>Sign in</html>", True),
    ],
)
def test_same_host_https_redirect_preserves_response_handling(
    mock_transport_client, status, content_type, body, expected_body, error
):
    sent = []

    def handler(request):
        sent.append(request)
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": f"https://{HOST}/result"}
            )
        return httpx.Response(
            status, headers={"Content-Type": content_type}, text=body
        )

    result = mock_transport_client(handler).request("GET", "/start")

    assert result["status"] == status
    assert result["url"] == f"https://{HOST}/result"
    assert result["body"] == expected_body
    assert bool(result.get("error")) is error
    assert len(sent) == 2
    assert all(request.headers["cookie"] == "t=synthetic" for request in sent)
    if content_type == "text/html":
        assert "cookie_refresh" in result["hint"]


class TestResolveRequestPath:
    def test_relative_gets_slash(self):
        assert resolve_request_path("api/x", HOST) == "/api/x"

    def test_absolute_path_unchanged(self):
        assert resolve_request_path("/api/x", HOST) == "/api/x"

    def test_full_url_same_host_allowed(self):
        url = f"https://{HOST}/api/x"
        assert resolve_request_path(url, HOST) == url

    def test_host_check_case_insensitive(self):
        assert resolve_request_path(f"https://{HOST.upper()}/api/x", HOST)

    def test_other_host_rejected(self):
        with pytest.raises(ValueError, match="Refusing to send"):
            resolve_request_path("https://evil.com/steal", HOST)

    def test_subdomain_confusion_rejected(self):
        with pytest.raises(ValueError):
            resolve_request_path(f"https://{HOST}.evil.com/x", HOST)

    def test_protocol_relative_rejected(self):
        # httpx would merge //evil.com/x over base_url into a real request.
        with pytest.raises(ValueError):
            resolve_request_path("//evil.com/x", HOST)

    def test_non_http_scheme_rejected(self):
        with pytest.raises(ValueError):
            resolve_request_path(f"ftp://{HOST}/x", HOST)

    def test_plain_http_rejected(self):
        # http:// to the right host would still send the cookie in cleartext.
        with pytest.raises(ValueError, match="Refusing to send"):
            resolve_request_path(f"http://{HOST}/api/x", HOST)


class TestParseCookieHeader:
    def test_basic(self):
        assert _parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"}

    def test_value_containing_equals(self):
        assert _parse_cookie_header("t=abc==; x=1") == {"t": "abc==", "x": "1"}

    def test_junk_parts_skipped(self):
        assert _parse_cookie_header("a=1; ; noequals; b=2") == {"a": "1", "b": "2"}

    def test_empty(self):
        assert _parse_cookie_header("") == {}


class TestLoadCookiesFromFile:
    def test_raw_header(self, tmp_path):
        p = tmp_path / "cookie.txt"
        p.write_text("t=abc; sid=xyz")
        assert _load_cookies_from_file(p) == {"t": "abc", "sid": "xyz"}

    def test_json_dict(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text('{"t": "abc", "sid": "xyz"}')
        assert _load_cookies_from_file(p) == {"t": "abc", "sid": "xyz"}

    def test_json_browser_export(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(
            '[{"name": "t", "value": "abc", "domain": "x"},'
            ' {"malformed": true},'
            ' {"name": "sid", "value": "xyz"}]'
        )
        assert _load_cookies_from_file(p) == {"t": "abc", "sid": "xyz"}

    def test_netscape(self, tmp_path):
        p = tmp_path / "cookies.txt"
        p.write_text(
            "# Netscape HTTP Cookie File\n"
            "x.myschoolapp.com\tFALSE\t/\tTRUE\t0\tt\tabc\n"
        )
        assert _load_cookies_from_file(p) == {"t": "abc"}

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "cookie.txt"
        p.write_text("   \n")
        with pytest.raises(RuntimeError, match="empty"):
            _load_cookies_from_file(p)

    def test_bad_json_raises(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text('{"t": ')
        with pytest.raises(RuntimeError, match="failed to parse"):
            _load_cookies_from_file(p)

    def test_unparseable_raises(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text('[{"malformed": true}]')
        with pytest.raises(RuntimeError, match="No cookies"):
            _load_cookies_from_file(p)


class TestLoadCookies:
    def test_dict_passthrough(self):
        assert _load_cookies({"t": "abc"}) == {"t": "abc"}

    def test_string_parsed(self):
        assert _load_cookies("t=abc; sid=xyz") == {"t": "abc", "sid": "xyz"}

    def test_empty_string_raises(self):
        with pytest.raises(RuntimeError, match="no name=value"):
            _load_cookies("garbage without pairs")

    def test_env_cookie(self, monkeypatch):
        monkeypatch.setenv("MSA_COOKIE", "t=abc")
        assert _load_cookies(None) == {"t": "abc"}

    def test_nothing_configured_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MSA_COOKIE", raising=False)
        monkeypatch.setenv("MSA_COOKIES_FILE", str(tmp_path / "nonexistent.txt"))
        with pytest.raises(RuntimeError, match="No cookies provided"):
            _load_cookies(None)


class TestCookieScoping:
    def test_jar_is_domain_scoped(self):
        client = MyschoolappClient(subdomain="testschool", cookies={"t": "abc"})
        try:
            domains = {c.domain for c in client._client.cookies.jar}
            assert domains == {"testschool.myschoolapp.com"}
        finally:
            client.close()

    def test_loaded_cookies_are_secure(self):
        client = MyschoolappClient(subdomain="testschool", cookies={"t": "synthetic"})
        try:
            assert all(cookie.secure for cookie in client._client.cookies.jar)
        finally:
            client.close()

    def test_missing_subdomain_raises(self, monkeypatch):
        monkeypatch.delenv("MSA_SUBDOMAIN", raising=False)
        with pytest.raises(RuntimeError, match="MSA_SUBDOMAIN"):
            MyschoolappClient(cookies={"t": "abc"})
