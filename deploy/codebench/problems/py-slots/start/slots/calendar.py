"""Free time in a day of meetings.

A meeting is a (start, end) pair of "HH:MM" strings (see times.py).
"""


def merge(meetings):
    """The meetings as the busy stretches they make: overlapping or touching
    meetings ('10:00'-'11:00' and '11:00'-'12:00') become one, and the result
    is sorted by start. A meeting whose end equals its start is empty and is
    dropped. A meeting that ends before it starts raises ValueError."""
    raise NotImplementedError


def free_slots(meetings, day_start="09:00", day_end="17:00", min_minutes=0):
    """The free stretches between day_start and day_end, sorted, as (start,
    end) pairs. Only a stretch at least min_minutes long is returned (exactly
    min_minutes counts). Meetings may start before the day or end after it;
    only the part inside the day is busy. A day_end before day_start raises
    ValueError."""
    raise NotImplementedError
