"""Category-weight breakdown: weighting, drops, extra credit, verification."""

from unittest.mock import Mock, patch

import pytest

with patch("myschoolapp_mcp.client.load_env_file", return_value=None):
    from myschoolapp_mcp import server


@pytest.fixture
def bd_client(monkeypatch):
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
                "groupownername": "Teacher",
                "currentterm": "1st Semester",
            }
            for sid in section_ids
        ],
    }


def assignment(
    aid,
    type_id,
    type_name,
    weight,
    max_points,
    *,
    drop=0,
    inc=True,
    extra_credit=False,
    title="Work",
):
    return {
        "AssignmentId": aid,
        "AssignmentIndexId": aid * 10,
        "AssignmentTypeId": type_id,
        "AssignmentType": type_name,
        "Weight": weight,
        "MaxPoints": max_points,
        "NumberToDrop": drop if drop else server.SENTINEL,
        "IncCumGrade": inc,
        "ExtraCredit": extra_credit,
        "AbbrDescription": title,
        "DateDue": "09/10",
    }


def grade(aid, earned=None, *, exempt=False, max_points=10.0):
    """Ungraded work omits PointsEarned entirely — mirror that, don't null it."""
    g = {
        "AssignmentId": aid,
        "AssignmentIndexId": aid * 10,
        "MaxPoints": max_points,
        "Exempt": exempt,
    }
    if earned is not None:
        g["PointsEarned"] = earned
    return g


def hydra(assignments, grades, section_grade=None):
    return {
        "status": 200,
        "body": {
            "Assignments": assignments,
            "Roster": [
                {
                    "StudentUserId": "123",
                    "SectionGrade": section_grade,
                    "AssignmentGrades": grades,
                }
            ],
        },
    }


def test_weights_normalize_when_they_do_not_sum_to_100(bd_client):
    """A 7/63 raw split is 10%/90% — the real Calc BC shape."""
    assignments = [
        assignment(1, 5772, "Minor items", 7.0, 10.0),
        assignment(2, 5773, "Major items", 63.0, 100.0),
    ]
    grades = [grade(1, 10.0), grade(2, 86.0, max_points=100.0)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, grades, section_grade=87.4),
    ]

    row = server.grade_breakdown(duration_id=1)["body"][0]

    assert row["verified"] is True
    assert row["computed_grade"] == pytest.approx(87.4)
    by_name = {c["category"]: c for c in row["categories"]}
    assert by_name["Minor items"]["weight_pct"] == pytest.approx(10.0)
    assert by_name["Major items"]["weight_pct"] == pytest.approx(90.0)
    assert by_name["Major items"]["contribution"] == pytest.approx(77.4)


def test_unverified_when_computed_diverges_from_posted(bd_client):
    """A total-points teacher must be flagged, not silently projected from."""
    assignments = [assignment(1, 1, "Work", 50.0, 10.0)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, [grade(1, 5.0)], section_grade=93.0),
    ]

    result = server.grade_breakdown(duration_id=1)

    assert result["body"][0]["verified"] is False
    assert result["body"][0]["delta"] == pytest.approx(-43.0)
    assert "does not match" in result["note"]


def test_no_posted_grade_is_not_reported_as_a_mismatch(bd_client):
    """An unpublished class has no claim to verify — don't cry wolf."""
    assignments = [assignment(1, 1, "Homework", 0.0, 10.0)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, [grade(1, 10.0)], section_grade=None),
    ]

    result = server.grade_breakdown(duration_id=1)

    assert result["body"][0]["posted_grade"] is None
    assert "note" not in result
    assert result["body"][0]["note"] == "no grade posted for this class yet"


def test_drop_lowest_removes_worst_ratio_not_worst_points(bd_client):
    """5/10 must outrank 80/100 as the drop — ratio, not raw point loss."""
    assignments = [
        assignment(1, 1, "Quiz", 100.0, 10.0, drop=1),
        assignment(2, 1, "Quiz", 100.0, 100.0, drop=1),
    ]
    grades = [grade(1, 5.0), grade(2, 80.0, max_points=100.0)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, grades, section_grade=80.0),
    ]

    cat = server.grade_breakdown(duration_id=1)["body"][0]["categories"][0]

    assert cat["drop_lowest"] == 1
    assert [d["max_points"] for d in cat["dropped"]] == [10.0]
    assert cat["points_earned"] == pytest.approx(80.0)
    assert cat["percent"] == pytest.approx(80.0)


def test_drop_is_skipped_when_it_would_empty_the_category(bd_client):
    assignments = [assignment(1, 1, "Quiz", 100.0, 10.0, drop=2)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, [grade(1, 7.0)], section_grade=70.0),
    ]

    cat = server.grade_breakdown(duration_id=1)["body"][0]["categories"][0]

    assert cat["dropped"] == []
    assert cat["percent"] == pytest.approx(70.0)


def test_extra_credit_adds_points_without_adding_possible(bd_client):
    assignments = [
        assignment(1, 1, "Work", 100.0, 10.0),
        assignment(2, 1, "Work", 100.0, 5.0, extra_credit=True),
    ]
    grades = [grade(1, 10.0), grade(2, 5.0, max_points=5.0)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, grades, section_grade=150.0),
    ]

    cat = server.grade_breakdown(duration_id=1)["body"][0]["categories"][0]

    assert cat["points_possible"] == pytest.approx(10.0)
    assert cat["points_earned"] == pytest.approx(15.0)
    assert cat["percent"] == pytest.approx(150.0)


def test_exempt_work_is_excluded_from_both_earned_and_ungraded(bd_client):
    assignments = [
        assignment(1, 1, "Work", 100.0, 10.0),
        assignment(2, 1, "Work", 100.0, 10.0, title="Excused"),
    ]
    grades = [grade(1, 9.0), grade(2, exempt=True)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, grades, section_grade=90.0),
    ]

    row = server.grade_breakdown(duration_id=1)["body"][0]

    assert row["categories"][0]["points_possible"] == pytest.approx(10.0)
    assert row["categories"][0]["ungraded_count"] == 0
    assert row["ungraded"] == []


def test_non_cumulative_assignments_are_ignored(bd_client):
    assignments = [
        assignment(1, 1, "Work", 100.0, 10.0),
        assignment(2, 1, "Work", 100.0, 999.0, inc=False, title="Practice"),
    ]
    grades = [grade(1, 10.0), grade(2, 0.0, max_points=999.0)]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, grades, section_grade=100.0),
    ]

    row = server.grade_breakdown(duration_id=1)["body"][0]

    assert row["categories"][0]["points_possible"] == pytest.approx(10.0)
    assert row["verified"] is True


def test_ungraded_category_holds_no_weight_until_first_grade(bd_client):
    """An empty Test category must not dilute the grade to 0."""
    assignments = [
        assignment(1, 1, "Homework", 20.0, 10.0),
        assignment(2, 2, "Test", 60.0, 100.0, title="Final"),
    ]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, [grade(1, 10.0)], section_grade=100.0),
    ]

    row = server.grade_breakdown(duration_id=1)["body"][0]
    by_name = {c["category"]: c for c in row["categories"]}

    assert row["computed_grade"] == pytest.approx(100.0)
    assert row["verified"] is True
    assert by_name["Test"]["weight_pct"] == 0.0
    assert by_name["Test"]["ungraded_points"] == pytest.approx(100.0)
    assert by_name["Homework"]["weight_pct"] == pytest.approx(100.0)


def test_ungraded_points_track_remaining_work(bd_client):
    assignments = [
        assignment(1, 1, "Minor", 7.0, 10.0),
        assignment(2, 1, "Minor", 7.0, 10.0, title="Upcoming"),
        assignment(3, 2, "Major", 63.0, 100.0, title="Test Ch. 4"),
    ]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, [grade(1, 10.0)], section_grade=100.0),
    ]

    row = server.grade_breakdown(duration_id=1)["body"][0]
    by_name = {c["category"]: c for c in row["categories"]}

    assert by_name["Minor"]["ungraded_points"] == pytest.approx(10.0)
    assert by_name["Major"]["ungraded_points"] == pytest.approx(100.0)
    assert {u["title"] for u in row["ungraded"]} == {"Upcoming", "Test Ch. 4"}


def test_unknown_section_id_raises_user_error(bd_client):
    bd_client.request.side_effect = [class_response(11)]

    with pytest.raises(server.UserError, match="No graded class"):
        server.grade_breakdown(section_id=999, duration_id=1)


def test_include_ungraded_false_omits_the_list(bd_client):
    assignments = [assignment(1, 1, "Work", 100.0, 10.0, title="Later")]
    bd_client.request.side_effect = [
        class_response(11),
        hydra(assignments, [], section_grade=None),
    ]

    row = server.grade_breakdown(duration_id=1, include_ungraded=False)["body"][0]

    assert "ungraded" not in row
    assert row["categories"][0]["ungraded_count"] == 1


def test_hydration_failure_marks_only_that_section(bd_client):
    assignments = [assignment(1, 1, "Work", 100.0, 10.0)]
    bd_client.request.side_effect = [
        class_response(11, 12),
        {"status": 403, "error": True, "body": {"message": "Forbidden"}},
        hydra(assignments, [grade(1, 10.0)], section_grade=100.0),
    ]

    result = server.grade_breakdown(duration_id=1)

    assert result["status"] == 207
    assert result["partial"] is True
    assert "error" in result["body"][0]
    assert result["body"][1]["verified"] is True
