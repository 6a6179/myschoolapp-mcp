"""Directory discovery uses synthetic context responses only."""

from unittest.mock import Mock, patch

import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


def test_directory_list_returns_only_directories_and_response_metadata(monkeypatch):
    directories = [
        {"DirectoryID": 42, "SortOrder": 1, "DirectoryName": "Students"},
        {"DirectoryID": 84, "SortOrder": 2, "DirectoryName": "Faculty"},
    ]
    response = {
        "status": 200,
        "url": "/api/webapp/context",
        "request_id": "synthetic-request",
        "body": {"Directories": directories, "SessionContext": "must stay private"},
    }
    client = Mock()
    client.request.return_value = response
    monkeypatch.setattr(server, "_get_client", lambda: client)

    assert server.directory_list() == {**response, "body": directories}
    client.request.assert_called_once_with("GET", "/api/webapp/context")
    assert response["body"]["SessionContext"] == "must stay private"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 403, "error": True, "body": {"SessionContext": "private"}},
        {"status": 200, "body": {"SessionContext": "private"}},
        {"status": 200, "body": "unexpected session context"},
        {"status": 200, "body": [{"SessionContext": "private"}]},
        {"status": 200},
        {"status": 200, "body": {"Directories": None, "SessionContext": "private"}},
        {"status": 200, "body": {"Directories": {"unexpected": "shape"}}},
    ],
)
def test_directory_list_reports_unexpected_shapes_without_context(monkeypatch, payload):
    response = {"url": "/api/webapp/context", **payload}
    client = Mock()
    client.request.return_value = response
    monkeypatch.setattr(server, "_get_client", lambda: client)

    result = server.directory_list()

    assert result["status"] == response["status"]
    assert result["url"] == response["url"]
    assert result["error"]
    if "error" in response:
        assert result["error"] == response["error"]
    body = response.get("body")
    expected = body.get("Directories") if isinstance(body, dict) else None
    assert result["body"] == expected
    assert "private" not in repr(result)
    assert "unexpected session context" not in repr(result)
