"""Assignment detail regressions using synthetic response wrappers only."""

from unittest.mock import Mock, patch

import httpx
import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


@pytest.fixture
def detail_client(monkeypatch):
    client = Mock(base_url="https://school.myschoolapp.com")
    monkeypatch.setattr(server, "_get_client", lambda: client)
    monkeypatch.setenv("MSA_STUDENT_ID", "123")
    return client


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        {"status": 403, "error": True, "body": {"message": "Forbidden"}},
        {"status": 403, "body": {"RubricId": 17}},
        {"status": 200, "error": "expired", "body": {"RubricId": 17}},
    ],
)
def test_detail_failure_preserves_wrapper_without_rubric_requests(
    detail_client, response, full
):
    detail_client.request.return_value = response

    assert server.assignment_detail(42, include_rubric=True, full=full) == {
        "detail": response
    }
    detail_client.request.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    [
        {"status": 403, "error": True, "body": {"message": "Forbidden"}},
        {"status": 503, "body": {"message": "Unavailable"}},
        {"status": 200, "error": "invalid", "body": {}},
    ],
)
def test_rubric_definition_failure_keeps_detail_and_results(detail_client, failure):
    detail_client.request.side_effect = [
        {"status": 200, "body": {"ShortDescription": "Essay", "RubricId": 17}},
        failure,
        {"status": 200, "body": [{"SkillName": "Clarity", "Points": 3}]},
    ]

    result = server.assignment_detail(42, include_rubric=True)

    assert result["assignment"]["title"] == "Essay"
    assert result["rubric"]["raw"] == failure
    assert "criteria" not in result["rubric"]
    assert result["rubric"]["results"][0]["points"] == 3


@pytest.mark.parametrize(
    "failure",
    [
        {"status": 403, "error": True, "body": {"message": "Forbidden"}},
        {"status": 503, "body": []},
        {"status": 200, "error": "invalid", "body": []},
        {"status": 200, "body": "unexpected results"},
    ],
)
def test_rubric_results_failure_keeps_detail_and_definition(detail_client, failure):
    detail_client.request.side_effect = [
        {"status": 200, "body": {"ShortDescription": "Essay", "RubricId": 17}},
        {"status": 200, "body": {"RubricId": 17, "Name": "Writing"}},
        failure,
    ]

    result = server.assignment_detail(42, include_rubric=True)

    assert result["assignment"]["title"] == "Essay"
    assert result["rubric"]["name"] == "Writing"
    assert result["rubric"]["results_error"] == failure
    assert "results" not in result["rubric"]


@pytest.mark.parametrize("rubric_id", [-2147483648, "-2147483648", -1, 0, "0", None])
def test_no_value_rubric_ids_skip_rubric_requests(detail_client, rubric_id):
    detail_client.request.return_value = {
        "status": 200,
        "body": {"ShortDescription": "Essay", "RubricId": rubric_id},
    }

    result = server.assignment_detail(42, include_rubric=True)

    assert result["assignment"]["title"] == "Essay"
    assert "rubric" not in result
    detail_client.request.assert_called_once()


@pytest.mark.parametrize("failed_component", ["definition", "results"])
@pytest.mark.parametrize("failure_type", [httpx.ConnectError, RuntimeError])
def test_rubric_transport_failure_keeps_available_components(
    detail_client, failed_component, failure_type
):
    definition = {"status": 200, "body": {"RubricId": 17, "Name": "Writing"}}
    results = {"status": 200, "body": [{"SkillName": "Clarity", "Points": 3}]}
    failure = failure_type("synthetic request failure")
    detail_client.request.side_effect = [
        {"status": 200, "body": {"ShortDescription": "Essay", "RubricId": 17}},
        failure if failed_component == "definition" else definition,
        failure if failed_component == "results" else results,
    ]

    result = server.assignment_detail(42, include_rubric=True)

    assert result["assignment"]["title"] == "Essay"
    if failed_component == "definition":
        assert result["rubric"]["raw"]["error"]
        assert result["rubric"]["results"][0]["points"] == 3
    else:
        assert result["rubric"]["name"] == "Writing"
        assert result["rubric"]["results_error"]["error"]
        assert "results" not in result["rubric"]
    assert detail_client.request.call_count == 3


def test_full_detail_keeps_raw_component_wrappers(detail_client):
    detail = {
        "status": 200,
        "body": {"ShortDescription": "Essay", "RubricId": 17, "ExtraField": 5},
    }
    rubric = {"status": 403, "error": True, "body": {"message": "Forbidden"}}
    results = {"status": 200, "body": []}
    detail_client.request.side_effect = [detail, rubric, results]

    assert server.assignment_detail(42, include_rubric=True, full=True) == {
        "detail": detail,
        "rubric": rubric,
        "rubric_results": results,
    }
