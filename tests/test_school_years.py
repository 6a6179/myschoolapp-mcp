"""Year discovery and per-request selection use synthetic responses only."""

from unittest.mock import Mock, patch

import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


@pytest.fixture
def year_client(monkeypatch):
    client = Mock()
    client.request.return_value = {"status": 200, "body": []}
    monkeypatch.setattr(server, "_get_client", lambda: client)
    monkeypatch.setenv("MSA_STUDENT_ID", "test-student")
    monkeypatch.setenv("MSA_PERSONA_ID", "3")
    monkeypatch.setenv("MSA_SCHOOL_YEAR", "2026 - 2027")
    return client


def test_school_years_preserves_exact_unique_labels_and_metadata(year_client):
    labels = ["2024 - 2025", " 2024 - 2025 ", "2026 - 2027", "Future term", ""]
    response = {
        "status": 200,
        "url": "/api/datadirect/StudentGradeLevelList/",
        "request_id": "synthetic-request",
        "body": [{"SchoolYearLabel": label} for label in [*labels, labels[0]]],
    }
    year_client.request.return_value = response

    assert server.school_years() == {**response, "body": labels}
    year_client.request.assert_called_once_with(
        "GET", "/api/datadirect/StudentGradeLevelList/"
    )
    assert len(response["body"]) == len(labels) + 1


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 403, "body": [{"SchoolYearLabel": "2024 - 2025"}]},
        {"status": 200, "error": True, "body": []},
        {"status": 200, "body": {"message": "unexpected shape"}},
        {"status": 200, "body": "unexpected shape"},
        {"status": 200, "body": None},
        {"status": 200},
        {"status": 200, "body": ["unexpected row"]},
        {"status": 200, "body": [{"SchoolYearLabel": None}]},
        {"status": 200, "body": [{"SchoolYearLabel": 2026}]},
        {"status": 200, "body": [{"GradeLevel": "12"}]},
    ],
)
def test_school_years_preserves_errors_and_unexpected_shapes(year_client, payload):
    response = {"url": "/api/datadirect/StudentGradeLevelList/", **payload}
    year_client.request.return_value = response

    assert server.school_years() is response
    year_client.request.assert_called_once_with(
        "GET", "/api/datadirect/StudentGradeLevelList/"
    )


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
def test_student_terms_selects_requested_school_year(year_client, school_year):
    result = server.student_terms(school_year=school_year)

    assert result is year_client.request.return_value
    year_client.request.assert_called_once_with(
        "GET",
        "/api/DataDirect/StudentGroupTermList/",
        params={
            "studentUserId": "test-student",
            "schoolYearLabel": "2026 - 2027" if school_year is None else school_year,
            "personaId": "3",
        },
    )


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
def test_duration_resolution_uses_requested_school_year(year_client, school_year):
    year_client.request.return_value = {
        "status": 200,
        "body": [{"OfferingType": 9, "CurrentInd": 1, "DurationId": 909}],
    }

    assert server._resolve_duration_id(9, school_year=school_year) == 909
    assert year_client.request.call_count == 1
    assert year_client.request.call_args.kwargs["params"]["schoolYearLabel"] == (
        "2026 - 2027" if school_year is None else school_year
    )


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
def test_fetch_classes_selects_requested_school_year(year_client, school_year):
    assert server._fetch_classes(404, "period", school_year) is (
        year_client.request.return_value
    )
    year_client.request.assert_called_once_with(
        "GET",
        "/api/datadirect/ParentStudentUserClassesGet",
        params={
            "userId": "test-student",
            "schoolYearLabel": "2026 - 2027" if school_year is None else school_year,
            "memberLevel": 3,
            "persona": "3",
            "durationList": 404,
            "markingPeriodId": "period",
        },
    )


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
@pytest.mark.parametrize("duration_id", [0, 777])
@pytest.mark.parametrize("current", [True, False])
def test_classes_keeps_selected_year_through_duration_resolution(
    year_client, school_year, duration_id, current
):
    def request(method, path, **kwargs):
        body = (
            [{"OfferingType": 1, "CurrentInd": current, "DurationId": 101}]
            if path.endswith("StudentGroupTermList/")
            else []
        )
        return {"status": 200, "body": body}

    year_client.request.side_effect = request

    if not duration_id and not current:
        with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
            server.classes(school_year=school_year)
        assert year_client.request.call_count == 1
    else:
        result = server.classes(duration_id, "period", False, school_year)
        assert result["duration_id"] == (duration_id or 101)
        assert year_client.request.call_count == (1 if duration_id else 2)
        assert year_client.request.call_args.kwargs["params"]["markingPeriodId"] == (
            "period"
        )
    for request_call in year_client.request.call_args_list:
        assert request_call.kwargs["params"]["schoolYearLabel"] == (
            "2026 - 2027" if school_year is None else school_year
        )


@pytest.mark.parametrize("status", [302, 403, 503])
def test_failed_historical_terms_never_supply_a_duration(year_client, status):
    year_client.request.return_value = {
        "status": status,
        "body": [{"OfferingType": 1, "CurrentInd": 1, "DurationId": 101}],
    }

    with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
        server.classes(school_year="2024 - 2025")

    assert year_client.request.call_count == 1
    assert year_client.request.call_args.kwargs["params"]["schoolYearLabel"] == (
        "2024 - 2025"
    )


@pytest.mark.parametrize("status", [None, "200"])
def test_school_years_preserves_malformed_status(year_client, status):
    response = {"status": status, "body": [{"SchoolYearLabel": "2024 - 2025"}]}
    year_client.request.return_value = response

    assert server.school_years() is response


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
def test_transcript_templates_selects_requested_school_year(year_client, school_year):
    assert server.transcript_templates(school_year) is year_client.request.return_value
    year_client.request.assert_called_once_with(
        "GET",
        "/api/Grading/StudentTranscriptTemplateList",
        params={
            "studentId": "test-student",
            "schoolYearLabel": "2026 - 2027" if school_year is None else school_year,
        },
    )


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
def test_attendance_selects_requested_school_year(year_client, school_year):
    assert server.attendance(school_year) is year_client.request.return_value
    year_client.request.assert_called_once_with(
        "GET",
        "/api/datadirect/ParentStudentUserAttendance/",
        params={
            "userId": "test-student",
            "personaId": "3",
            "schoolYearLabel": "2026 - 2027" if school_year is None else school_year,
        },
    )


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
def test_conduct_selects_requested_school_year(year_client, school_year):
    assert server.conduct(2, school_year) is year_client.request.return_value
    year_client.request.assert_called_once_with(
        "GET",
        "/api/datadirect/ParentStudentUserConduct/",
        params={
            "studentUserId": "test-student",
            "viewerPersonaId": "3",
            "schoolYearLabel": "2026 - 2027" if school_year is None else school_year,
            "levelNum": 2,
        },
    )


@pytest.mark.parametrize(
    ("kind", "offering", "duration_param"),
    [("advisory", 3, "durationId"), ("athletic", 9, "durationList"),
     ("community", 11, "durationId")],
)
@pytest.mark.parametrize("school_year", [None, "2024 - 2025"])
@pytest.mark.parametrize("duration_id", [0, 777])
@pytest.mark.parametrize("current", [True, False])
def test_group_membership_keeps_selected_year_through_duration_resolution(
    year_client, kind, offering, duration_param, school_year, duration_id, current
):
    def request(method, path, **kwargs):
        body = (
            [{"OfferingType": offering, "CurrentInd": current, "DurationId": 909}]
            if path.endswith("StudentGroupTermList/")
            else []
        )
        return {"status": 200, "body": body}

    year_client.request.side_effect = request
    auto_resolve = not duration_id and kind != "community"

    if auto_resolve and not current:
        with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
            server.group_membership(kind, duration_id, school_year)
        assert year_client.request.call_count == 1
    else:
        assert server.group_membership(kind, duration_id, school_year)["status"] == 200
        assert year_client.request.call_count == (2 if auto_resolve else 1)
        assert year_client.request.call_args.kwargs["params"][duration_param] == (
            909 if auto_resolve else duration_id
        )
    for request_call in year_client.request.call_args_list:
        assert request_call.kwargs["params"]["schoolYearLabel"] == (
            "2026 - 2027" if school_year is None else school_year
        )
