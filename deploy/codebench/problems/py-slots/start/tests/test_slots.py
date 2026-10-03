import pytest

from slots.calendar import free_slots, merge
from slots.times import fmt, parse


def test_parse_and_fmt():
    assert parse("09:30") == 570
    assert parse("00:00") == 0
    assert fmt(570) == "09:30"
    assert fmt(parse("23:59")) == "23:59"


def test_parse_rejects():
    with pytest.raises(ValueError):
        parse("25:00")


def test_merge_overlapping():
    assert merge([("10:00", "11:30"), ("09:00", "10:30")]) == [("09:00", "11:30")]


def test_free_slots():
    meetings = [("10:00", "11:00"), ("13:00", "14:30")]
    assert free_slots(meetings) == [("09:00", "10:00"), ("11:00", "13:00"), ("14:30", "17:00")]


def test_free_slots_minimum():
    meetings = [("09:20", "12:00"), ("12:15", "16:00")]
    assert free_slots(meetings, min_minutes=30) == [("16:00", "17:00")]
