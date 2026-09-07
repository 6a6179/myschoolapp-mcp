"""Server regressions; no school session, local credentials, or network needed."""

import os
from unittest.mock import Mock, call, patch

import pytest

# Server import normally loads .env; tests must never read local credentials.
with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


MODERN_REPORT_PATH = "/api/Grading/StudentReportCardTemplateList"
LEGACY_REPORT_PATH = "/api/datadirect/ParentStudentUserPerformance/"


@pytest.fixture
def report_client(monkeypatch):
    client = Mock()
    monkeypatch.setattr(server, "_get_client", lambda: client)
    monkeypatch.setenv("MSA_STUDENT_ID", "test-student")
    monkeypatch.setenv("MSA_PERSONA_ID", "3")
    monkeypatch.setenv("MSA_SCHOOL_YEAR", "2026 - 2027")
    return client


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 200, "body": [{"Id": "modern-template"}]},
        {"status": 403, "error": True, "body": []},
        {"status": 503, "body": []},
        {"status": 302, "body": []},
        {"status": 200, "error": True, "body": []},
        {"status": 200, "error": True, "body": "<html>Login</html>"},
        {"status": 200, "body": {}},
        {"status": 200, "body": None},
        {"status": 200, "body": ""},
        {"status": 200, "body": False},
        {"status": 200, "body": ()},
        {"status": 200},
    ],
)
def test_report_card_templates_preserves_modern_response(report_client, payload):
    response = {"url": MODERN_REPORT_PATH, **payload}
    report_client.request.return_value = response

    assert server.report_card_templates() is response
    report_client.request.assert_called_once_with(
        "GET",
        MODERN_REPORT_PATH,
        params={"studentId": "test-student", "schoolYearLabel": "2026 - 2027"},
    )


def test_report_card_templates_falls_back_to_legacy_reports(report_client):
    report = {
        "performance_type": "Report",
        "performance_description": "Trimester 3 Report Card",
        "Id": "legacy-report",
        "format": "pdf",
        "rc_type": "fixture-type",
    }
    rows = [
        {"performance_type": "Transcript", "Id": "unrelated"},
        report,
        {"performance_type": "report", "Id": "wrong-case"},
        {"Id": "missing-type"},
    ]
    legacy = {"status": 200, "url": LEGACY_REPORT_PATH, "body": rows}
    report_client.request.side_effect = [
        {"status": 200, "url": MODERN_REPORT_PATH, "body": []},
        legacy,
    ]

    result = server.report_card_templates()

    assert result == {**legacy, "body": [report], "source": "legacy"}
    assert legacy["body"] == rows  # Do not mutate the original response.
    assert report_client.request.call_args_list == [
        call(
            "GET",
            MODERN_REPORT_PATH,
            params={"studentId": "test-student", "schoolYearLabel": "2026 - 2027"},
        ),
        call(
            "GET",
            LEGACY_REPORT_PATH,
            params={
                "userId": "test-student",
                "personaId": "3",
                "schoolYearLabel": "2026 - 2027",
            },
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 403, "error": True, "body": []},
        {"status": 503, "body": []},
        {"status": 302, "body": []},
        {"status": 200, "error": True, "body": []},
        {"status": 200, "error": True, "body": "<html>Login</html>"},
        {"status": 200, "body": {"message": "unexpected shape"}},
        {"status": 200, "body": None},
        {"status": 200, "body": ""},
        {"status": 200},
    ],
)
def test_report_card_templates_preserves_legacy_failure(report_client, payload):
    response = {"url": LEGACY_REPORT_PATH, **payload}
    report_client.request.side_effect = [{"status": 200, "body": []}, response]

    assert server.report_card_templates() is response
    assert report_client.request.call_count == 2


@pytest.mark.parametrize("school_year", [None, "2025 - 2026", ""])
@pytest.mark.parametrize("modern_body", [[], [{"Id": "modern-template"}]])
def test_report_card_templates_school_year(report_client, school_year, modern_body):
    report_client.request.side_effect = [
        {"status": 200, "url": MODERN_REPORT_PATH, "body": modern_body},
        {"status": 200, "url": LEGACY_REPORT_PATH, "body": []},
    ]

    result = server.report_card_templates(school_year=school_year)

    assert result["status"] == 200
    assert report_client.request.call_count == (1 if modern_body else 2)
    expected_year = "2026 - 2027" if school_year is None else school_year
    for request in report_client.request.call_args_list:
        assert request.kwargs["params"]["schoolYearLabel"] == expected_year


def test_duration_routing():
    terms = [
        {"OfferingType": 3, "DurationId": 303, "CurrentInd": 1},
        {"OfferingType": 1, "DurationId": 100, "CurrentInd": 0},
        {"OfferingType": 1, "DurationId": 101, "CurrentInd": 1},
        {"OfferingType": 2, "DurationId": 202, "CurrentInd": 1},
        {"OfferingType": 4, "DurationId": 404, "CurrentInd": 1},
        {"OfferingType": 9, "DurationId": 909, "CurrentInd": 1},
        {"OfferingType": 11, "DurationId": 0, "CurrentInd": 1},
    ]

    def request(method, path, **kwargs):
        assert method == "GET"
        body = terms if path.endswith("StudentGroupTermList/") else []
        return {"status": 200, "body": body}

    client = Mock()
    client.request.side_effect = request
    cases = [
        (server.classes, {}, "durationList", 101),
        (server.gradebook, {}, "durationList", 101),
        (server.group_membership, {"kind": "advisory"}, "durationId", 303),
        (server.group_membership, {"kind": "athletic"}, "durationList", 909),
        (server.group_membership, {"kind": "dorm"}, "durationId", 404),
        (server.group_membership, {"kind": "activity"}, "durationId", 202),
        (server.group_membership, {"kind": "community"}, "durationId", 0),
    ]
    with (
        patch.object(server, "_get_client", return_value=client),
        patch.dict(os.environ, {"MSA_STUDENT_ID": "test-student"}),
    ):
        for tool, args, duration_param, expected in cases:
            for override in (None, 0, 999):
                client.request.reset_mock()
                kwargs = args if override is None else {**args, "duration_id": override}
                assert tool(**kwargs)["status"] == 200
                params = client.request.call_args.kwargs["params"]
                assert params[duration_param] == (override or expected)
                auto_resolved = not override and args.get("kind") != "community"
                assert client.request.call_count == (2 if auto_resolved else 1)

        client.request.side_effect = None
        for response in [
            {"status": 200, "body": []},
            {"status": 200, "body": terms[:2]},
            {"status": 200, "body": "not a term list"},
            {"status": 403, "error": True, "body": terms},
        ]:
            client.request.return_value = response
            with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
                server._resolve_duration_id()

        client.request.reset_mock()
        client.request.return_value = {"status": 200, "body": terms[:3]}
        with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
            server.group_membership("athletic")
        assert client.request.call_count == 1
