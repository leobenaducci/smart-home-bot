import pytest

from slots.calendar import free_slots, merge
from slots.times import fmt, parse


@pytest.mark.parametrize("bad", ["9:30", "24:00", "09:60", " 09:30", "09:30 ", "0930", "ab:cd", "", "09:3", "-1:00"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse(bad)


@pytest.mark.parametrize("bad", [-1, 1440, 5000])
def test_fmt_rejects(bad):
    with pytest.raises(ValueError):
        fmt(bad)


def test_round_trip():
    for m in range(0, 1440, 7):
        assert parse(fmt(m)) == m


def test_merge_touching_and_contained():
    got = merge([("11:00", "12:00"), ("10:00", "11:00"), ("10:15", "10:45"), ("15:00", "16:00")])
    assert got == [("10:00", "12:00"), ("15:00", "16:00")]


def test_merge_empty_and_backwards():
    assert merge([("10:00", "10:00")]) == []
    assert merge([]) == []
    with pytest.raises(ValueError):
        merge([("11:00", "10:00")])


def test_whole_day_free_and_busy():
    assert free_slots([]) == [("09:00", "17:00")]
    assert free_slots([("08:00", "18:00")]) == []


def test_meetings_outside_the_day():
    meetings = [("07:00", "09:30"), ("16:30", "19:00"), ("06:00", "07:00")]
    assert free_slots(meetings) == [("09:30", "16:30")]


def test_minimum_is_inclusive():
    meetings = [("09:30", "10:00"), ("10:30", "17:00")]
    assert free_slots(meetings, min_minutes=30) == [("09:00", "09:30"), ("10:00", "10:30")]
    assert free_slots(meetings, min_minutes=31) == []


def test_custom_day():
    assert free_slots([("12:00", "13:00")], day_start="11:00", day_end="14:00") == [("11:00", "12:00"), ("13:00", "14:00")]
    with pytest.raises(ValueError):
        free_slots([], day_start="12:00", day_end="11:00")


def test_unsorted_overlapping_input():
    meetings = [("14:00", "15:00"), ("09:00", "09:45"), ("09:30", "10:00"), ("14:30", "14:45")]
    assert free_slots(meetings) == [("10:00", "14:00"), ("15:00", "17:00")]
