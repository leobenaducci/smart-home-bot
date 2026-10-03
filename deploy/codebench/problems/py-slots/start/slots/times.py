"""Times of day as "HH:MM" strings, on a 24-hour clock, within one day."""


def parse(text):
    """'09:30' -> 570, the minutes after midnight.

    Exactly two digits, a colon, two digits; the hour 00-23 and the minute
    00-59. Anything else -- '9:30', '24:00', '09:60', ' 09:30' -- raises
    ValueError.
    """
    raise NotImplementedError


def fmt(minutes):
    """570 -> '09:30'. The inverse of parse(), for 0 <= minutes < 1440;
    ValueError outside that range."""
    raise NotImplementedError
