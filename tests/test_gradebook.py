"""Gradebook hydration regressions with synthetic classes and rosters."""

from unittest.mock import Mock, patch

import httpx
import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


@pytest.fixture
def grade_client(monkeypatch):
    client = Mock()
    monkeypatch.setattr(server, "_get_client", lambda: client)
    monkeypatch.setenv("MSA_STUDENT_ID", "123")
    monkeypatch.setenv("MSA_SCHOOL_YEAR", "2026 - 2027")
    return client


def class_response(*section_ids):
    return {
        "status": 200,
        "body": [
            {
                "leadsectionid": sid,
                "markingperiodid": 3,
                "sectionidentifier": f"Class {sid}",
                "cumgrade": "75",
            }
            for sid in section_ids
        ],
    }


def roster_response(student_id="123", current=85, year=90):
    return {
        "status": 200,
        "body": {
            "Roster": [
                {
                    "StudentUserId": student_id,
                    "SectionGrade": current,
                    "SectionGradeYear": year,
                }
            ]
        },
    }


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        {"status": 403, "error": True, "body": {"message": "Forbidden"}},
        {"status": 403, "body": {"message": "Forbidden"}},
        {"status": 200, "error": "expired", "body": {"Roster": []}},
    ],
)
def test_hydration_failure_preserves_other_sections(grade_client, failure, full):
    grade_client.request.side_effect = [
        class_response(11, 22), failure, roster_response()
    ]

    result = server.gradebook(duration_id=7, full=full)

    assert result["status"] == 207
    assert result["error"]
    assert result["partial"] is True
    failed, successful = result["body"]
    assert failed["section_id"] == 11
    assert failed["error"]
    assert failed["hydrate_error"] == failure
    assert failed["hydration_verified"] is False
    assert successful["section_id"] == 22
    assert successful["current_grade"] == 85
    assert successful["year_grade"] == 90
    assert successful["hydration_verified"] is True
    assert "error" not in successful
    if full:
        assert failed["hydrate"] == failure["body"]
        assert successful["hydrate"] == roster_response()["body"]


@pytest.mark.parametrize(
    "body",
    [
        roster_response(student_id="other-student")["body"],
        {"Roster": []},
        {},
        {"Roster": None},
        {"Roster": [{"SectionGrade": 99, "SectionGradeYear": 100}]},
        {"Roster": ["unexpected"]},
        {"Roster": {"StudentUserId": "123", "SectionGrade": 99}},
    ],
)
def test_roster_requires_verified_student_identity(grade_client, body):
    hydra = {"status": 200, "body": body}
    grade_client.request.side_effect = [class_response(11), hydra]

    result = server.gradebook(duration_id=7)

    assert result["status"] == 502
    assert result["error"]
    assert result["partial"] is False
    row = result["body"][0]
    assert row["error"]
    assert row["hydration_verified"] is False
    assert row["hydrate_error"] == hydra
    assert row["current_grade"] == 75  # Only the class-list fallback is usable.
    assert row["year_grade"] is None


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("synthetic connection failure"),
        RuntimeError("Request failed: ConnectError: synthetic connection failure"),
    ],
)
def test_transport_failure_keeps_successful_sections(grade_client, failure):
    grade_client.request.side_effect = [
        class_response(11, 22),
        failure,
        roster_response(),
    ]

    result = server.gradebook(duration_id=7)

    assert result["status"] == 207
    assert result["partial"] is True
    failed, successful = result["body"]
    assert failed["error"]
    assert failed["hydration_verified"] is False
    assert successful["current_grade"] == 85
    assert successful["year_grade"] == 90
    assert successful["hydration_verified"] is True
    assert grade_client.request.call_count == 3


def test_unavailable_year_grade_zero_is_not_a_failing_grade(grade_client):
    # Live hydration uses SectionGradeYear=0 when no YTD grade is published.
    grade_client.request.side_effect = [class_response(11), roster_response(year=0)]
    result = server.gradebook(duration_id=7)
    assert result["body"][0]["year_grade"] is None
    assert result["body"][0]["year_grade_display"] is None


@pytest.mark.parametrize("zero", [0, "0"])
def test_hydrated_current_zero_grades_are_preserved(grade_client, zero):
    grade_client.request.side_effect = [
        class_response(11), roster_response(current=zero)
    ]

    result = server.gradebook(duration_id=7)

    assert result["status"] == 200
    row = result["body"][0]
    assert row["current_grade"] == 0.0
    assert row["current_grade_display"] == "0.00%"
    assert row["year_grade"] == 90.0
    assert row["year_grade_display"] == "90.00%"


@pytest.mark.parametrize("display,expected", [("", None), ("0%", 0.0)])
def test_class_fallback_distinguishes_zero_placeholder(grade_client, display, expected):
    classes = class_response(11)
    classes["body"][0].update(cumgrade=0, CumulativeDisplay=display)
    grade_client.request.side_effect = [
        classes, {"status": 403, "body": {"message": "Forbidden"}}
    ]

    result = server.gradebook(duration_id=7)

    row = result["body"][0]
    assert row["current_grade"] == expected
    assert row["current_grade_display"] == (None if expected is None else "0.00%")


@pytest.mark.parametrize("school_year", [None, "2024 - 2025", ""])
@pytest.mark.parametrize("duration_id", [0, 7])
def test_school_year_reaches_terms_and_classes(grade_client, school_year, duration_id):
    terms = {"status": 200, "body": [
        {"OfferingType": 1, "DurationId": 7, "CurrentInd": 1}
    ]}
    grade_client.request.side_effect = (
        [terms, class_response()] if not duration_id else [class_response()]
    )

    result = server.gradebook(duration_id=duration_id, school_year=school_year)

    assert result["status"] == 200
    assert result["duration_id"] == 7
    assert grade_client.request.call_count == (2 if not duration_id else 1)
    for request in grade_client.request.call_args_list:
        assert request.kwargs["params"]["schoolYearLabel"] == (
            "2026 - 2027" if school_year is None else school_year
        )


def test_historical_gradebook_does_not_guess_term(grade_client):
    grade_client.request.return_value = {
        "status": 200,
        "body": [{"OfferingType": 1, "DurationId": 7, "CurrentInd": 0}],
    }

    with pytest.raises(RuntimeError, match="Pass duration_id explicitly"):
        server.gradebook(school_year="2024 - 2025")

    grade_client.request.assert_called_once()
    assert grade_client.request.call_args.kwargs["params"]["schoolYearLabel"] == (
        "2024 - 2025"
    )


@pytest.mark.parametrize("full", [False, True])
def test_class_enumeration_failure_is_preserved(grade_client, full):
    failure = {"status": 403, "url": "/synthetic/classes", "body": []}
    grade_client.request.return_value = failure

    assert server.gradebook(duration_id=7, full=full) is failure
    grade_client.request.assert_called_once()
