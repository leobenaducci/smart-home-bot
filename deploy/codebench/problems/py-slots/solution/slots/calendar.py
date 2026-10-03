from .times import fmt, parse


def _merged(meetings):
    spans = []
    for start, end in meetings:
        s, e = parse(start), parse(end)
        if e < s:
            raise ValueError("a meeting ends before it starts")
        if e > s:
            spans.append((s, e))
    spans.sort()
    out = []
    for s, e in spans:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def merge(meetings):
    return [(fmt(s), fmt(e)) for s, e in _merged(meetings)]


def free_slots(meetings, day_start="09:00", day_end="17:00", min_minutes=0):
    lo, hi = parse(day_start), parse(day_end)
    if hi < lo:
        raise ValueError("the day ends before it starts")
    free, cur = [], lo
    for s, e in _merged(meetings):
        s, e = max(s, lo), min(e, hi)
        if e <= s:
            continue
        if s > cur:
            free.append((cur, s))
        cur = max(cur, e)
    if hi > cur:
        free.append((cur, hi))
    return [(fmt(s), fmt(e)) for s, e in free if e - s >= min_minutes]
