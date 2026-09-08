"""Small remaining audit regressions; all fixtures are synthetic."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from myschoolapp_mcp.client import MyschoolappClient, _load_cookies_from_file


def test_netscape_httponly_records_are_not_comments():
    export = (
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_school.myschoolapp.com\tFALSE\t/\tTRUE\t0\tsession\tfixture\n"
    )
    with patch.object(Path, "read_text", return_value=export):
        assert _load_cookies_from_file(Path("synthetic.cookies")) == {
            "session": "fixture"
        }


def test_html_links_keep_urls_for_supported_attribute_quotes():
    from myschoolapp_mcp.formatting import strip_html

    expected = "worksheet (https://files.invalid/task.pdf)"
    assert (
        strip_html("<a href='https://files.invalid/task.pdf'>worksheet</a>") == expected
    )
    assert (
        strip_html("<a href=https://files.invalid/task.pdf>worksheet</a>") == expected
    )


def test_assignment_resource_urls_preserve_valid_references():
    with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
        from myschoolapp_mcp import server
    client = Mock(base_url="https://school.myschoolapp.com")
    for value, expected in [
        ("//cdn.invalid/task.pdf", "https://cdn.invalid/task.pdf"),
        ("HTTPS://files.invalid/task.pdf", "HTTPS://files.invalid/task.pdf"),
        ("mailto:teacher@example.invalid", "mailto:teacher@example.invalid"),
        ("/files/a.pdf", "https://school.myschoolapp.com/files/a.pdf"),
    ]:
        assert server._absolute_url(client, value) == expected


def test_empty_gradebook_section_filter_fetches_no_grades():
    with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
        from myschoolapp_mcp import server
    client = Mock()
    client.request.return_value = {"status": 200, "body": {"Roster": []}}
    rows = {"status": 200, "body": [{"leadsectionid": 10, "markingperiodid": 20}]}
    with (
        patch.object(server, "_get_client", return_value=client),
        patch.object(server, "_student_id", return_value="100"),
        patch.object(server, "_fetch_classes", return_value=rows),
    ):
        result = server.gradebook(duration_id=1, section_ids=[])
    assert result["body"] == []
    client.request.assert_not_called()


@pytest.mark.parametrize("kind", ["json", "netscape"])
def test_cookie_file_import_does_not_rescope_foreign_domains(
    tmp_path, monkeypatch, kind
):
    entries = [
        {"domain": "school.myschoolapp.com", "name": "session", "value": "school"},
        {"domain": ".myschoolapp.com", "name": "parent", "value": "parent"},
        {"domain": "login.example.invalid", "name": "idp", "value": "foreign"},
        {"domain": "login.example.invalid", "name": "session", "value": "wrong"},
    ]
    path = tmp_path / "synthetic.cookies"
    if kind == "json":
        path.write_text(json.dumps(entries))
    else:
        path.write_text(
            "\n".join(
                f"{e['domain']}\tFALSE\t/\tTRUE\t0\t{e['name']}\t{e['value']}"
                for e in entries
            )
        )
    monkeypatch.delenv("MSA_COOKIE", raising=False)
    monkeypatch.setenv("MSA_COOKIES_FILE", str(path))
    client = MyschoolappClient(subdomain="school")
    try:
        assert dict(client._client.cookies) == {"session": "school", "parent": "parent"}
    finally:
        client.close()
