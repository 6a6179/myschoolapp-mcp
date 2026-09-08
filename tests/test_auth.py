"""Cookie export regressions with fake Playwright and synthetic credentials."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from myschoolapp_mcp.client import _load_cookies_from_file

# Auth imports this function for later use, so patch before importing it.
with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import auth


@pytest.fixture
def fake_login(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "load_env_file", lambda: None)
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    for name, value in {
        "MSA_SUBDOMAIN": "testschool",
        "SCHOOL_EMAIL": "synthetic@example.invalid",
        "SCHOOL_PASS": "synthetic-password",
        "HEADLESS": "True",
        "TIMEOUT": "100",
        "MSA_COOKIES_FILE": str(tmp_path / "export" / "cookie.txt"),
    }.items():
        monkeypatch.setenv(name, value)

    context = Mock()
    browser = Mock()
    browser.new_context.return_value = context
    playwright = MagicMock()
    playwright.__enter__.return_value.chromium.launch.return_value = browser
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: playwright),
    )
    return context


def test_export_excludes_foreign_identity_cookies(fake_login):
    school_origin = "https://testschool.myschoolapp.com"
    school_cookies = [
        {"name": "session", "value": "school", "domain": "testschool.myschoolapp.com"},
        {"name": "shared", "value": "school-parent", "domain": ".myschoolapp.com"},
    ]
    foreign_cookies = [
        {"name": "session", "value": "foreign", "domain": "login.microsoftonline.com"},
        {"name": "identity", "value": "foreign", "domain": "app.blackbaud.com"},
        {"name": "other-school", "value": "foreign", "domain": "other.myschoolapp.com"},
    ]

    def browser_selected_cookies(urls=None):
        if urls is None:
            return school_cookies + foreign_cookies
        assert urls == [school_origin]
        return school_cookies

    fake_login.cookies.side_effect = browser_selected_cookies

    path = auth.refresh_cookie()

    assert _load_cookies_from_file(path) == {
        "session": "school",
        "shared": "school-parent",
    }
    fake_login.cookies.assert_called_once_with([school_origin])
    assert path.stat().st_mode & 0o777 == 0o600


def test_empty_school_cookie_selection_preserves_prior_file(fake_login):
    prior_path = auth._cookie_output_path()
    prior_path.parent.mkdir(parents=True)
    prior_path.write_text("session=prior")
    fake_login.cookies.return_value = []

    with pytest.raises(RuntimeError, match=r"No cookies.*school"):
        auth.refresh_cookie()

    assert prior_path.read_text() == "session=prior"
