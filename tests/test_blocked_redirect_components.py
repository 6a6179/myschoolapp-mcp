"""Blocked redirects must preserve usable components without sending off-site."""

from unittest.mock import patch

import httpx
import pytest

from myschoolapp_mcp.client import MyschoolappClient

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


HOST = "testschool.myschoolapp.com"
BLOCKED_DESTINATIONS = [
    "https://other-school.myschoolapp.com/blocked",
    f"http://{HOST}/blocked",
]
DETAIL_PATH = "/api/assignment2/UserAssignmentDetailsGetAllStudentData"
RUBRIC_PATH = "/api/Rubric/AssignmentRubric/"
RESULTS_PATH = "/api/Rubric/RubricResultsGet/"
CLASSES_PATH = "/api/datadirect/ParentStudentUserClassesGet"
HYDRATE_PATH = "/api/gradebook/hydrategradebook"


@pytest.fixture
def offline_client(monkeypatch):
    """Keep the real client and HTTPX redirect hooks, replacing only transport."""
    original_client = httpx.Client
    clients = []
    monkeypatch.setenv("MSA_STUDENT_ID", "123")
    monkeypatch.setenv("MSA_PERSONA_ID", "2")

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
        monkeypatch.setattr(server, "_get_client", lambda: client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.mark.parametrize("destination", BLOCKED_DESTINATIONS)
@pytest.mark.parametrize("blocked_component", ["rubric", "rubric_results"])
@pytest.mark.parametrize("full", [False, True])
def test_blocked_rubric_redirect_preserves_available_components(
    offline_client, destination, blocked_component, full
):
    detail = {"ShortDescription": "Synthetic essay", "RubricId": 17}
    definition = {"RubricId": 17, "Name": "Synthetic writing rubric"}
    scores = [{"SkillName": "Clarity", "Points": 3}]
    bodies = {DETAIL_PATH: detail, RUBRIC_PATH: definition, RESULTS_PATH: scores}
    blocked_path = RUBRIC_PATH if blocked_component == "rubric" else RESULTS_PATH
    sent = []

    def handler(request):
        sent.append(request)
        assert request.url.scheme == "https" and request.url.host == HOST
        if request.url.path == blocked_path:
            return httpx.Response(302, headers={"Location": destination})
        return httpx.Response(200, json=bodies[request.url.path])

    offline_client(handler)
    result = server.assignment_detail(42, include_rubric=True, full=full)

    assert [request.url.path for request in sent] == [
        DETAIL_PATH, RUBRIC_PATH, RESULTS_PATH
    ]
    assert all(request.headers["cookie"] == "t=synthetic" for request in sent)
    if full:
        assert result["detail"]["status"] == 200
        assert result["detail"]["body"] == detail
        failure = result[blocked_component]
        successful_component = (
            "rubric_results" if blocked_component == "rubric" else "rubric"
        )
        assert result[successful_component]["status"] == 200
        assert result[successful_component]["body"] == (
            scores if blocked_component == "rubric" else definition
        )
    else:
        assert result["assignment"]["title"] == "Synthetic essay"
        if blocked_component == "rubric":
            failure = result["rubric"]["raw"]
            assert "criteria" not in result["rubric"]
            assert result["rubric"]["results"][0]["points"] == 3
        else:
            failure = result["rubric"]["results_error"]
            assert result["rubric"]["name"] == "Synthetic writing rubric"
            assert "results" not in result["rubric"]
    assert failure["status"] is None
    assert failure["body"] is None
    assert failure["url"] == blocked_path
    assert failure["error"].startswith("Component transport failure: ")
    assert destination not in str(result)
    assert "t=synthetic" not in str(result)


@pytest.mark.parametrize("destination", BLOCKED_DESTINATIONS)
@pytest.mark.parametrize("blocked_section", [11, 22])
@pytest.mark.parametrize("full", [False, True])
def test_blocked_hydration_redirect_preserves_other_sections(
    offline_client, destination, blocked_section, full
):
    classes = [
        {"leadsectionid": sid, "markingperiodid": 3, "cumgrade": "75"}
        for sid in (11, 22)
    ]
    roster = {
        "Roster": [{"StudentUserId": "123", "SectionGrade": 85, "SectionGradeYear": 90}]
    }
    sent = []

    def handler(request):
        sent.append(request)
        assert request.url.scheme == "https" and request.url.host == HOST
        if request.url.path == CLASSES_PATH:
            return httpx.Response(200, json=classes)
        assert request.url.path == HYDRATE_PATH
        if request.url.params["sectionId"] == str(blocked_section):
            return httpx.Response(302, headers={"Location": destination})
        return httpx.Response(200, json=roster)

    offline_client(handler)
    result = server.gradebook(duration_id=7, school_year="2026 - 2027", full=full)

    assert [request.url.path for request in sent] == [
        CLASSES_PATH, HYDRATE_PATH, HYDRATE_PATH
    ]
    assert [request.url.params["sectionId"] for request in sent[1:]] == ["11", "22"]
    assert all(request.headers["cookie"] == "t=synthetic" for request in sent)
    assert result["status"] == 207
    assert result["partial"] is True
    assert result["error"] == "Could not verify gradebook for 1 section(s)"
    assert [row["section_id"] for row in result["body"]] == [11, 22]
    for row in result["body"]:
        if row["section_id"] == blocked_section:
            assert row["hydration_verified"] is False
            assert row["current_grade"] == 75
            assert row["year_grade"] is None
            assert row["error"]
            failure = row["hydrate_error"]
            assert failure["status"] is None
            assert failure["body"] is None
            assert failure["error"].startswith("hydrategradebook transport failure: ")
            if full:
                assert row["hydrate"] is None
        else:
            assert row["hydration_verified"] is True
            assert row["current_grade"] == 85
            assert row["year_grade"] == 90
            assert "error" not in row
            if full:
                assert row["hydrate"] == roster
    assert destination not in str(result)
    assert "t=synthetic" not in str(result)


@pytest.mark.parametrize("destination", BLOCKED_DESTINATIONS)
def test_invalid_initial_url_still_raises_value_error_before_send(
    offline_client, destination
):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    client = offline_client(handler)
    with pytest.raises(ValueError, match="Refusing to send") as raised:
        client.request("GET", destination)
    assert type(raised.value) is ValueError
    assert sent == []


@pytest.mark.parametrize("operation", ["assignment", "gradebook"])
def test_unrelated_component_value_error_is_not_swallowed(offline_client, operation):
    failure = ValueError("synthetic non-boundary failure")

    def handler(request):
        if request.url.path == DETAIL_PATH:
            return httpx.Response(200, json={"RubricId": 17})
        if request.url.path == CLASSES_PATH:
            return httpx.Response(
                200, json=[{"leadsectionid": 11, "markingperiodid": 3}]
            )
        raise failure

    offline_client(handler)
    with pytest.raises(ValueError, match="synthetic non-boundary failure") as raised:
        if operation == "assignment":
            server.assignment_detail(42, include_rubric=True)
        else:
            server.gradebook(duration_id=7, school_year="2026 - 2027")
    assert raised.value is failure
