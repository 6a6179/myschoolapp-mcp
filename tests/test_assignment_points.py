"""assignments(): max_points / points_earned come from hydrategradebook.

The DataDirect list endpoint carries no point values at all (verified
against a live Tabor deployment — no maxpoints/MaxPoints key anywhere in
the payload). hydrategradebook does: `Assignments[].MaxPoints` for every
assignment in the (section, marking period) and
`Roster[me].AssignmentGrades[].PointsEarned` once graded.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from myschoolapp_mcp import server

LIST_PATH = "/api/DataDirect/AssignmentCenterAssignments/"
HYDRATE_PATH = "/api/gradebook/hydrategradebook"
STUDENT = "7848335"


def _item(idx, section, mp, title, due, major=False, **extra):
    return {
        "groupname": f"Class {section}",
        "section_id": section,
        "assignment_id": idx * 10,
        "assignment_index_id": idx,
        "short_description": title,
        "date_assigned": "9/4/2026 8:00 AM",
        "date_due": due,
        "assignment_type": "Major items" if major else "Minor items",
        "assignment_status": 1,
        "missing_ind": False,
        "late_ind": False,
        "incomplete_ind": False,
        "major": major,
        "has_grade": False,
        "drop_box_ind": False,
        "marking_period_id": mp,
        **extra,
    }


def _hydrate(section, assignments, grades):
    return {
        "status": 200,
        "url": HYDRATE_PATH,
        "body": {
            "Roster": [
                {
                    "StudentUserId": 999,
                    "AssignmentGrades": [{"AssignmentIndexId": 1, "PointsEarned": 0.0}],
                },
                {"StudentUserId": int(STUDENT), "AssignmentGrades": grades},
            ],
            "Assignments": assignments,
            "Summary": {"GroupName": f"Class {section}"},
        },
    }


@pytest.fixture
def today():
    return server._today()


@pytest.fixture
def client(monkeypatch, today):
    """Fake client: one list response (+ empty missing/overdue), per-section hydrate."""
    monkeypatch.setenv("MSA_STUDENT_ID", STUDENT)
    due_tomorrow = today + timedelta(days=1)
    due_str = f"{due_tomorrow.month}/{due_tomorrow.day}/{due_tomorrow.year} 8:45 AM"
    items = [
        _item(1, 100, 21758, "Test Chapter 3", due_str, major=True),
        _item(2, 100, 21758, "Review Handout", due_str),
        _item(3, 200, 21758, "Spanish thing", due_str),
        _item(4, 300, None, "Wellness block, no gradebook", due_str),
    ]
    hydrate = {
        ("100", "21758"): _hydrate(
            100,
            [
                {"AssignmentIndexId": 1, "MaxPoints": 100.0},
                {"AssignmentIndexId": 2, "MaxPoints": 10.0},
            ],
            [
                {
                    "AssignmentIndexId": 1,
                    "MaxPoints": 100.0,
                },  # ungraded: no PointsEarned
                {
                    "AssignmentIndexId": 2,
                    "MaxPoints": 10.0,
                    "PointsEarned": 0.0,
                },  # real zero
            ],
        ),
        ("200", "21758"): {
            "status": 403,
            "url": HYDRATE_PATH,
            "body": {"Error": "nope"},
        },
    }
    calls = []

    def request(method, path, params=None, **kw):
        calls.append((path, params))
        if path == LIST_PATH:
            return {
                "status": 200,
                "url": LIST_PATH,
                "body": items if params["filter"] == 0 else [],
            }
        if path == HYDRATE_PATH:
            return hydrate[(str(params["sectionId"]), str(params["markingPeriodId"]))]
        raise AssertionError(f"unexpected call {path}")

    fake = MagicMock()
    fake.request.side_effect = request
    fake.calls = calls
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    return fake


def _rows(out):
    return {r["title"]: r for rows in out["buckets"].values() for r in rows}


def test_points_attached_from_gradebook(client):
    out = server.assignments()
    rows = _rows(out)

    assert rows["Test Chapter 3"]["max_points"] == 100.0
    assert "points_earned" not in rows["Test Chapter 3"]  # not graded yet
    assert rows["Review Handout"]["max_points"] == 10.0
    assert rows["Review Handout"]["points_earned"] == 0.0  # real zero survives

    # major_assignments is a separate compact copy; it must get points too.
    major = {r["title"]: r for r in out["major_assignments"]}
    assert major["Test Chapter 3"]["max_points"] == 100.0


def test_one_hydrate_call_per_section_marking_period(client):
    server.assignments()
    hydrate_calls = [p for path, p in client.calls if path == HYDRATE_PATH]
    pairs = {(str(p["sectionId"]), str(p["markingPeriodId"])) for p in hydrate_calls}
    # Section 300 has no marking period -> no gradebook -> no call.
    assert pairs == {("100", "21758"), ("200", "21758")}
    assert len(hydrate_calls) == 2
    assert all(p["studentUserId"] == STUDENT for p in hydrate_calls)


def test_failed_gradebook_degrades_to_note(client):
    out = server.assignments()
    rows = _rows(out)
    assert "max_points" not in rows["Spanish thing"]
    assert "max_points" not in rows["Wellness block, no gradebook"]
    assert out["note"] == "points unavailable for: Class 200"
    # The list itself still came through intact.
    assert out["counts"]["DueTomorrow"] == 4


def test_with_points_false_skips_gradebook(client):
    out = server.assignments(with_points=False)
    assert not [p for p, _ in client.calls if p == HYDRATE_PATH]
    assert all("max_points" not in r for r in _rows(out).values())
    assert "note" not in out


def test_by_class_view_also_gets_points(client):
    out = server.assignments(display_by_due_date=False)
    rows = _rows(out)
    assert rows["Test Chapter 3"]["max_points"] == 100.0


def test_hydrate_transport_failure_is_a_note_not_a_crash(client, monkeypatch):
    def boom(client_, path, params):
        return {
            "status": None,
            "error": "Component transport failure: ReadTimeout",
            "body": None,
        }

    monkeypatch.setattr(server, "_request_assignment_component", boom)
    out = server.assignments()
    assert out["note"] == "points unavailable for: Class 100, Class 200"
    assert out["counts"]["DueTomorrow"] == 4
