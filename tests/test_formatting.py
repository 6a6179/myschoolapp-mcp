"""Unit tests for the pure formatting helpers. No network, no env."""

from datetime import date

import pytest

from myschoolapp_mcp.formatting import (
    ASSIGNMENT_BUCKETS,
    DEFAULT_ASSIGNMENT_BUCKETS,
    SENTINEL,
    assignment_field,
    clean_schedule_item,
    compact_assignment,
    compact_class,
    compact_directory_entry,
    due_bucket,
    fmt_pct,
    format_range,
    mdy,
    or_none,
    parse_assignment_date,
    status_label,
    strip_html,
    to_float,
)


class TestMdy:
    def test_converts_iso(self):
        assert mdy("2026-08-17") == "8/17/2026"

    def test_no_zero_padding(self):
        assert mdy("2026-01-05") == "1/5/2026"

    def test_empty_and_none(self):
        assert mdy(None) == ""
        assert mdy("") == ""

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="expected YYYY-MM-DD"):
            mdy("08/17/2026")
        with pytest.raises(ValueError):
            mdy("not a date")


class TestOrNone:
    def test_empty_string_becomes_none(self):
        assert or_none("") is None

    def test_zero_and_false_survive(self):
        assert or_none(0) == 0
        assert or_none(False) is False

    def test_values_pass_through(self):
        assert or_none("85") == "85"
        assert or_none(None) is None


class TestBuckets:
    def test_default_excludes_historical(self):
        assert "PastBeforeLastWeek" not in DEFAULT_ASSIGNMENT_BUCKETS
        assert set(DEFAULT_ASSIGNMENT_BUCKETS) < set(ASSIGNMENT_BUCKETS)


class TestAssignmentField:
    def test_lowercase_key(self):
        assert assignment_field({"short_description": "Essay"}, "title") == "Essay"

    def test_pascal_fallback(self):
        assert assignment_field({"ShortDescription": "Essay"}, "title") == "Essay"

    def test_missing_is_none(self):
        assert assignment_field({}, "title") is None
        assert assignment_field({"x": 1}, "not_a_field") is None


class TestCompactAssignment:
    def test_maps_known_fields(self):
        item = {
            "assignment_index_id": 41609904,
            "groupname": "English 10",
            "short_description": "Essay",
            "date_due": "9/15/2026 11:59 PM",
            "junk_field": "dropped",
        }
        out = compact_assignment(item)
        assert out["assignment_index_id"] == 41609904
        assert out["class"] == "English 10"
        assert out["title"] == "Essay"
        assert out["due"] == "9/15/2026 11:59 PM"
        assert "junk_field" not in out

    def test_unknown_schema_passes_through(self):
        item = {"WeirdKey": 1, "OtherKey": "x"}
        assert compact_assignment(item) == item


class TestParseAssignmentDate:
    def test_datetime_format(self):
        assert parse_assignment_date("9/15/2026 11:59 PM") == date(2026, 9, 15)

    def test_date_only(self):
        assert parse_assignment_date("9/15/2026") == date(2026, 9, 15)

    def test_iso_prefix(self):
        assert parse_assignment_date("2026-09-15T23:59:00") == date(2026, 9, 15)

    def test_garbage(self):
        assert parse_assignment_date("soon") is None
        assert parse_assignment_date(None) is None
        assert parse_assignment_date("") is None
        assert parse_assignment_date(12345) is None


class TestDueBucket:
    # 2026-09-16 is a Wednesday; its week runs Mon 9/14 - Sun 9/20.
    TODAY = date(2026, 9, 16)

    def test_today_and_tomorrow(self):
        assert due_bucket(date(2026, 9, 16), self.TODAY) == "DueToday"
        assert due_bucket(date(2026, 9, 17), self.TODAY) == "DueTomorrow"

    def test_this_week(self):
        assert due_bucket(date(2026, 9, 18), self.TODAY) == "DueThisWeek"
        assert due_bucket(date(2026, 9, 20), self.TODAY) == "DueThisWeek"

    def test_next_week(self):
        assert due_bucket(date(2026, 9, 21), self.TODAY) == "DueNextWeek"
        assert due_bucket(date(2026, 9, 27), self.TODAY) == "DueNextWeek"

    def test_after_next_week(self):
        assert due_bucket(date(2026, 9, 28), self.TODAY) == "DueAfterNextWeek"

    def test_past_this_week(self):
        assert due_bucket(date(2026, 9, 14), self.TODAY) == "PastThisWeek"
        assert due_bucket(date(2026, 9, 15), self.TODAY) == "PastThisWeek"

    def test_past_last_week(self):
        assert due_bucket(date(2026, 9, 7), self.TODAY) == "PastLastWeek"
        assert due_bucket(date(2026, 9, 13), self.TODAY) == "PastLastWeek"

    def test_ancient_history(self):
        assert due_bucket(date(2026, 9, 6), self.TODAY) == "PastBeforeLastWeek"
        assert due_bucket(date(2025, 1, 1), self.TODAY) == "PastBeforeLastWeek"


class TestStatusLabel:
    def test_sentinel_is_todo(self):
        assert status_label(SENTINEL) == "To do"

    def test_known_codes(self):
        assert status_label(4) == "Graded"
        assert status_label(-1) == "To do"

    def test_string_code_coerced(self):
        assert status_label("4") == "Graded"

    def test_unknown_and_none(self):
        assert status_label(None) is None
        assert status_label(99) is None
        assert status_label("nope") is None


class TestStripHtml:
    def test_plain_passthrough(self):
        assert strip_html("hello world") == "hello world"

    def test_none_and_empty(self):
        assert strip_html(None) is None
        assert strip_html("") is None
        assert strip_html("   ") is None
        assert strip_html(42) is None

    def test_anchor_keeps_url(self):
        out = strip_html('Read <a href="https://x.com/doc">the doc</a> now')
        assert out == "Read the doc (https://x.com/doc) now"

    def test_list_items(self):
        out = strip_html("<ul><li>one</li><li>two</li></ul>")
        assert "- one" in out
        assert "- two" in out

    def test_entities_and_blocks(self):
        out = strip_html("<p>Fish &amp; Chips</p><p>Second</p>")
        assert "Fish & Chips" in out
        assert "\n\n" in out

    def test_collapses_whitespace(self):
        out = strip_html("<div>a    b</div>\n\n\n\n<div>c</div>")
        assert "a b" in out
        assert "\n\n\n" not in out


class TestGradeParsing:
    def test_fmt_pct(self):
        assert fmt_pct(85.39) == "85.39%"
        assert fmt_pct("90.7") == "90.70%"

    def test_zero_means_ungraded(self):
        assert fmt_pct(0) is None
        assert to_float(0) is None
        assert to_float("0") is None

    def test_empty_and_garbage(self):
        assert fmt_pct(None) is None
        assert to_float("") is None
        assert to_float("N/A") is None

    def test_real_values(self):
        assert to_float("85.5") == 85.5
        assert to_float(100) == 100.0


class TestFormatRange:
    def test_range(self):
        assert format_range(3.1, 4) == "3.1-4"

    def test_collapsed(self):
        assert format_range(4, 4) == "4"
        assert format_range(4, None) == "4"
        assert format_range(None, 4) == "4"

    def test_empty(self):
        assert format_range(None, None) is None


class TestCleanScheduleItem:
    def test_basic_class(self):
        item = {
            "CourseTitle": "English 10",
            "Block": "C",
            "MyDayStartTime": "9:00 AM",
            "MyDayEndTime": "9:50 AM",
            "RoomNumber": "204",
            "BuildingName": "",
            "Contact": "Jane Doe",
            "ContactEmail": "jdoe@school.org",
            "SectionId": 12345,
            "AttendanceDisplay": "Attended",
            "AssignmentCount": SENTINEL,
            "StartTime": "1/1/1900 9:00 AM",
        }
        out = clean_schedule_item(item)
        assert out["class"] == "English 10"
        assert out["attendance"] == "Attended"
        assert "building" not in out  # empty string dropped
        assert "AssignmentCount" not in out
        assert "StartTime" not in out

    def test_sentinel_dropped(self):
        out = clean_schedule_item({"CourseTitle": "X", "SectionId": SENTINEL})
        assert "section_id" not in out

    def test_athletics_fields(self):
        item = {
            "CourseTitle": "JV Soccer",
            "ScheduleItemType": "Practice",
            "Opponent": "Rival Academy",
            "AthHomeAway": "Away",
            "CanceledInd": True,
        }
        out = clean_schedule_item(item)
        assert out["type"] == "Practice"
        assert out["opponent"] == "Rival Academy"
        assert out["home_away"] == "Away"
        assert out["canceled"] is True

    def test_no_athletics_keys_for_classes(self):
        out = clean_schedule_item({"CourseTitle": "Math"})
        assert "opponent" not in out
        assert "canceled" not in out


class TestCompactClass:
    def test_key_mapping(self):
        c = {
            "sectionid": 1,
            "leadsectionid": 2,
            "sectionidentifier": "Bio - 3",
            "groupownername": "J. Doe",
            "cumgrade": "91.2",
            "coursedescription": "<p>huge html blob</p>",
        }
        out = compact_class(c)
        assert out == {
            "section_id": 1,
            "lead_section_id": 2,
            "class": "Bio - 3",
            "teacher": "J. Doe",
            "current_grade": 91.2,
        }

    def test_zero_grade_means_no_grade(self):
        # cumgrade of 0 is the API's "no grade yet", not a real 0%.
        out = compact_class({"cumgrade": 0, "CumulativeDisplay": ""})
        assert out == {"current_grade": None, "current_grade_display": None}


class TestCompactDirectoryEntry:
    def test_strips_and_drops(self):
        row = {
            "UserID": 100,
            "UserNameFormatted": "Mrs. Maddy Smith '09  ",
            "Email": "msmith@school.org",
            "OfficePhone": "555-1234    ",
            "JobTitle": "Teacher",
            "DepartmentDisplay": "",
            "GradYear": "",
            "IsStudentInd": False,
        }
        out = compact_directory_entry(row)
        assert out["name"] == "Mrs. Maddy Smith '09"
        assert out["phone"] == "555-1234"
        assert "department" not in out
        assert "grad_year" not in out
        assert out["is_student"] is False
