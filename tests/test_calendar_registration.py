"""MCP calendar wrapper forwards to the independently tested read helper."""

from unittest.mock import Mock, patch

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


def test_calendar_tool_forwards_explicit_request_without_changing_it():
    assert callable(getattr(server, "calendar_events", None))
    client = Mock()
    expected = {"status": 200, "body": []}
    with (
        patch.object(server, "_get_client", return_value=client),
        patch(
            "myschoolapp_mcp.calendar_tools.fetch_calendar_events",
            return_value=expected,
        ) as fetch,
    ):
        result = server.calendar_events(
            "2026-09-01", "2026-09-07", ["synthetic_filter"], True, True
        )
    assert result is expected
    fetch.assert_called_once_with(
        client,
        "2026-09-01",
        "2026-09-07",
        calendar_ids=["synthetic_filter"],
        include_practice=True,
        full=True,
    )
