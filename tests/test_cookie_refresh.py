"""Refresh adoption uses only synthetic cookies and offline HTTP transport."""

import asyncio
import os
from unittest.mock import patch

import httpx
import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import auth, server


@pytest.fixture
def session(monkeypatch, tmp_path):
    async def run_fake_refresh(func):
        return func()

    # The login is fake; avoid worker-thread/event-loop teardown in the sandbox.
    monkeypatch.setattr(server.asyncio, "to_thread", run_fake_refresh)
    monkeypatch.setattr(auth, "load_env_file", lambda: None)
    monkeypatch.setenv("MSA_SUBDOMAIN", "testschool")
    monkeypatch.setenv("MSA_STUDENT_ID", "synthetic-student")
    monkeypatch.setenv("MSA_COOKIE", "session=stale")
    monkeypatch.setenv("MSA_COOKIES_FILE", str(tmp_path / "prior-cookie.txt"))
    monkeypatch.setattr(server, "_client", None)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"synthetic": True})

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler), trust_env=False, **kwargs
        ),
    )
    old_client = server._get_client()
    fresh_path = tmp_path / "fresh-cookie.txt"
    fresh_path.write_text("session=fresh")
    yield old_client, fresh_path, sent
    server._drop_client()


def test_successful_refresh_uses_fresh_file_over_stale_env(monkeypatch, session):
    old_client, fresh_path, sent = session
    monkeypatch.setattr(auth, "refresh_cookie", lambda: fresh_path)

    result = asyncio.run(server.cookie_refresh())
    profile = server.whoami()

    assert result == {"ok": True, "cookie_path": str(fresh_path)}
    assert profile["body"] == {"synthetic": True}
    assert sent[0].headers["cookie"] == "session=fresh"
    assert old_client._client.is_closed
    assert server._get_client() is not old_client


@pytest.mark.parametrize("failure", ["login", "empty_file", "client_init"])
def test_failed_refresh_preserves_prior_session(monkeypatch, session, failure):
    old_client, fresh_path, sent = session

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic refresh failure")

    if failure == "login":
        monkeypatch.setattr(auth, "refresh_cookie", fail)
    else:
        monkeypatch.setattr(auth, "refresh_cookie", lambda: fresh_path)
        if failure == "empty_file":
            fresh_path.write_text("")
        else:
            monkeypatch.setattr(server, "MyschoolappClient", fail)

    with pytest.raises(RuntimeError):
        asyncio.run(server.cookie_refresh())

    assert server._get_client() is old_client
    assert not old_client._client.is_closed
    assert os.environ["MSA_COOKIE"] == "session=stale"
    assert os.environ["MSA_COOKIES_FILE"] == str(fresh_path.parent / "prior-cookie.txt")
    assert server.whoami()["body"] == {"synthetic": True}
    assert sent[0].headers["cookie"] == "session=stale"
