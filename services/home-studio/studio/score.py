"""A song's parts written out: guitar as notes and tablature, piano on two
staves, from the notes the Studio's audio.cpp hears in it (MuScriptor).

MuScriptor answers with note events in seconds -- each a pitch, a start, an
end and the instrument it heard. This turns them into a score a person can
read and practise from:

  * **The grid comes from the notes.** The song's beats (librosa) give the
    tempo within a few percent, and a few percent is a second and a half of
    drift by the end of a 90-second song: on the first song tried the beat
    tracker said 123 bpm and the strums fell every 0.2475 s, 121.2 bpm. So the
    beat length and phase are fitted to the note onsets themselves, starting
    from the tracker's tempo, and the bar starts where the chords change.
  * **One voice per staff.** Notes that start together are a chord, and a
    chord lasts until the next one starts or until its notes end -- which is
    how a strummed part and a picked line both read. Overlaps a single voice
    cannot show are cut at the next onset.
  * **Tablature is a choice of string and fret for every note**, and a chord
    shape is chosen as a whole: frets within a hand's reach, open strings
    welcome, and close to where the hand already is. Standard tuning; a note
    the guitar cannot play is left out of the tab rather than invented.

The MusicXML written here is what the practice page renders (alphaTab) and
what can be opened in MuseScore; the page's cursor follows the song by the
same tempo and start time, which travel beside it in the score's meta.
"""
from __future__ import annotations

import cmath
import itertools
import math
from collections import Counter, defaultdict
from xml.sax.saxutils import escape

# What the Studio asks MuScriptor to hear (its own instrument group names).
GUITARS = {"acoustic_guitar": "Acoustic guitar", "clean_electric_guitar": "Electric guitar",
           "distorted_electric_guitar": "Distorted guitar"}
PIANOS = {"acoustic_piano": "Piano", "electric_piano": "Electric piano"}
INSTRUMENTS = (*GUITARS, *PIANOS)
MIDI_PROGRAM = {"acoustic_guitar": 25, "clean_electric_guitar": 28, "distorted_electric_guitar": 31,
                "acoustic_piano": 1, "electric_piano": 5}

# Standard tuning, low to high, as MIDI pitches; strings are numbered from the
# high E (1) to the low E (6), as tablature numbers them.
TUNING = (40, 45, 50, 55, 59, 64)
MAX_FRET = 20
SPAN = 4                     # frets a hand reaches without moving (open strings aside)
SUB = 4                      # grid steps per beat: sixteenth notes
BAR = 4 * SUB                # 4/4
MIN_NOTES = 12               # an instrument heard fewer times than this is noise


def notes_from_events(events: list[dict]) -> list[dict]:
    """MuScriptor's start/end events, paired into notes."""
    starts = {e["index"]: e for e in events if e.get("type") == "start"}
    out = []
    for e in events:
        if e.get("type") != "end":
            continue
        s = starts.pop(e.get("start_event_index"), None)
        if s is None:
            continue
        out.append({"instrument": s.get("instrument") or "", "pitch": int(s["pitch"]),
                    "start": float(s["start_time"]), "end": max(float(e["end_time"]), float(s["start_time"]))})
    for s in starts.values():                               # never closed: a short one
        out.append({"instrument": s.get("instrument") or "", "pitch": int(s["pitch"]),
                    "start": float(s["start_time"]), "end": float(s["start_time"]) + 0.25})
    return sorted(out, key=lambda n: (n["start"], n["pitch"]))


# -- the grid ---------------------------------------------------------------------
def fit_grid(notes: list[dict], tempo_hint: float) -> tuple[float, float]:
    """(seconds per beat, time of a bar's first beat), fitted to the onsets.

    The period: the one around the hint whose sixteenth grid the onsets sit
    on most tightly (the length of their mean phase vector). The beat's phase:
    of the four sixteenths, the one the most onsets land on. The bar's: of the
    four beats, the one the most chord changes land on -- a change of harmony
    is what a downbeat usually carries."""
    onsets = sorted({round(n["start"], 2) for n in notes})
    if len(onsets) < 8:
        beat = 60.0 / (tempo_hint or 120.0)
        return beat, 0.0
    hint = 60.0 / min(200.0, max(60.0, tempo_hint or 120.0))
    best = (-1.0, hint)
    for i in range(-300, 301):
        p = hint * (1 + i / 5000)                          # +-6 %, in steps of 0.02 %
        z = sum(cmath.exp(2j * math.pi * t * SUB / p) for t in onsets) / len(onsets)
        if abs(z) > best[0]:
            best = (abs(z), p)
    beat = best[1]
    step = beat / SUB
    z = sum(cmath.exp(2j * math.pi * t / step) for t in onsets)
    phase = (cmath.phase(z) / (2 * math.pi)) % 1 * step     # a sixteenth grid point
    idx = [round((t - phase) / step) for t in onsets]
    beat_off = Counter(k % SUB for k in idx).most_common(1)[0][0]
    changes = _chord_changes(notes)
    bar_votes = Counter()
    for t in changes:
        k = round((t - phase) / step)
        if k % SUB == beat_off:
            bar_votes[(k // SUB) % 4] += 1
    bar_beat = bar_votes.most_common(1)[0][0] if bar_votes else 0
    first = phase + (beat_off + bar_beat * SUB) * step
    return beat, first % (BAR * step)


def _chord_changes(notes: list[dict]) -> list[float]:
    by = defaultdict(set)
    for n in notes:
        by[round(n["start"], 2)].add(n["pitch"] % 12)
    out, last = [], None
    for t in sorted(by):
        if len(by[t]) >= 3 and by[t] != last:
            out.append(t)
            last = by[t]
    return out


# -- spelling -------------------------------------------------------------------------
SHARP = ("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0), ("F", 1), ("G", 0), ("G", 1), ("A", 0), ("A", 1), ("B", 0)
FLAT = ("C", 0), ("D", -1), ("D", 0), ("E", -1), ("E", 0), ("F", 0), ("G", -1), ("G", 0), ("A", -1), ("A", 0), ("B", -1), ("B", 0)
MAJOR = (0, 2, 4, 5, 7, 9, 11)
FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 5: -1, 10: -2, 3: -3, 8: -4, 1: -5, 6: 6}


def key_of(notes: list[dict]) -> int:
    """The key signature (fifths) whose major scale holds the most notes --
    a relative minor shares it, which is all a signature says."""
    hist = Counter(n["pitch"] % 12 for n in notes)
    tonic = max(range(12), key=lambda k: (sum(hist[(k + d) % 12] for d in MAJOR), -abs(FIFTHS[k])))
    return FIFTHS[tonic]


def spell(pitch: int, fifths: int) -> tuple[str, int, int]:
    step, alter = (FLAT if fifths < 0 else SHARP)[pitch % 12]
    octave = (pitch - alter) // 12 - 1
    return step, alter, octave


CHORDS = (((0, 4, 7), "major", ""), ((0, 3, 7), "minor", "m"), ((0, 4, 7, 10), "dominant", "7"),
          ((0, 4, 7, 11), "major-seventh", "maj7"), ((0, 3, 7, 10), "minor-seventh", "m7"),
          ((0, 2, 7), "suspended-second", "sus2"), ((0, 5, 7), "suspended-fourth", "sus4"),
          ((0, 3, 6), "diminished", "dim"), ((0, 4, 8), "augmented", "aug"), ((0, 7), "power", "5"))


def chord_name(pitches: list[int]) -> tuple[int, str, str] | None:
    """(root pitch class, MusicXML kind, suffix) for a set of notes, or None.
    The bass note is tried as the root first: an open G shape is G, not Em7."""
    pcs = {p % 12 for p in pitches}
    if len(pcs) < 2:
        return None
    bass = min(pitches) % 12
    for root in [bass] + sorted(pcs - {bass}):
        rel = frozenset((p - root) % 12 for p in pcs)
        for shape, kind, suffix in CHORDS:
            if rel == frozenset(shape):
                return root, kind, suffix
    return None


# -- voices -------------------------------------------------------------------------------
def events_on_grid(notes: list[dict], beat: float, first: float) -> tuple[list[dict], int]:
    """Chords on the grid: [{at, length, pitches}] in sixteenths from the first
    bar, and how many sixteenths before `first` the score starts (a pickup
    moves the start back a bar)."""
    step = beat / SUB
    lead = BAR if notes and min(n["start"] for n in notes) < first - step / 2 else 0
    by = defaultdict(lambda: {"pitches": set(), "end": 0})
    for n in notes:
        at = round((n["start"] - first) / step) + lead
        end = max(at + 1, round((n["end"] - first) / step) + lead)
        if at < 0:
            continue
        by[at]["pitches"].add(n["pitch"])
        by[at]["end"] = max(by[at]["end"], end)
    starts = sorted(by)
    out = []
    for i, at in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else at + BAR
        out.append({"at": at, "length": max(1, min(by[at]["end"], nxt) - at), "pitches": sorted(by[at]["pitches"])})
    return out, lead


# -- tablature ------------------------------------------------------------------------------
def _placements(pitches: list[int]) -> list[tuple[tuple[int, int], ...]]:
    """Every way to put these pitches on distinct strings: ((string, fret), ...)
    in the pitches' order. Strings are 1 (high E) to 6 (low E)."""
    options = []
    for p in pitches:
        opts = [(6 - i, p - open_) for i, open_ in enumerate(TUNING) if 0 <= p - open_ <= MAX_FRET]
        options.append(opts)
    out = []
    for combo in itertools.product(*options):
        strings = [s for s, _ in combo]
        if len(set(strings)) != len(strings):
            continue
        fretted = [f for _, f in combo if f > 0]
        if fretted and max(fretted) - min(fretted) > SPAN - 1:
            continue
        out.append(combo)
        if len(out) > 400:
            break
    return out


def _cost(combo, hand: float) -> tuple[float, float]:
    fretted = [f for _, f in combo if f > 0]
    pos = min(fretted) if fretted else hand
    spread = (max(fretted) - min(fretted)) if fretted else 0
    return spread * 1.0 + pos * 0.25 + abs(pos - hand) * 0.6, pos


def fret_chords(events: list[dict]) -> list[dict]:
    """A string and fret for each note, chord shape by chord shape, by the
    cheapest path through the whole part (hand movement, reach, height).

    A chord that cannot be fingered whole loses notes -- the doubled ones
    first, then from the middle -- until it can; what is left out of the tab
    stays in the notation, which is what the person hears."""
    layers = []
    for ev in events:
        pitches = sorted(set(ev["pitches"]))[-6:]
        combos = _placements(pitches)
        while not combos and len(pitches) > 1:
            pcs = Counter(p % 12 for p in pitches)
            doubled = [p for p in pitches[1:-1] if pcs[p % 12] > 1]
            drop = doubled[0] if doubled else pitches[len(pitches) // 2]
            pitches = [p for p in pitches if p != drop]
            combos = _placements(pitches)
        layers.append((pitches, combos or [()]))
    # Viterbi over hand positions.
    prev = {None: (0.0, [])}
    for pitches, combos in layers:
        cur = {}
        for combo in combos:
            best = None
            for hand, (total, path) in prev.items():
                c, pos = _cost(combo, 0.0 if hand is None else hand)
                if best is None or total + c < best[0]:
                    best = (total + c, path + [(pitches, combo)], pos)
            key = best[2]
            if key not in cur or best[0] < cur[key][0]:
                cur[key] = (best[0], best[1])
        # Keep the few cheapest hands: the path cost is what decides.
        prev = dict(sorted(cur.items(), key=lambda kv: kv[1][0])[:24])
    path = min(prev.values(), key=lambda v: v[0])[1] if prev else []
    out = []
    for ev, (pitches, combo) in zip(events, path):
        frets = {p: sf for p, sf in zip(pitches, combo)}
        out.append({**ev, "frets": frets})
    return out


# -- MusicXML --------------------------------------------------------------------------------
TYPES = {16: ("whole", 0), 12: ("half", 1), 8: ("half", 0), 6: ("quarter", 1), 4: ("quarter", 0),
         3: ("eighth", 1), 2: ("eighth", 0), 1: ("16th", 0)}


def _pieces(pos: int, length: int) -> list[int]:
    """A length in sixteenths from `pos` in the bar, as note values that read:
    long values only where they start on a beat that carries them."""
    out = []
    while length > 0:
        for v in (16, 12, 8, 6, 4, 3, 2, 1):
            if v > length or pos + v > BAR:
                continue
            if (v == 16 and pos) or (v in (12, 8) and pos % 4) or (v in (6, 4) and pos % 2):
                continue
            out.append(v)
            pos += v
            length -= v
            break
    return out


def _measures(events: list[dict], total: int) -> list[list[tuple[int, int, dict | None, str]]]:
    """Per bar: (position, value, chord or None for a rest, tie) -- with every
    chord and rest cut at barlines and beats into values that read."""
    bars = [[] for _ in range(max(1, math.ceil(total / BAR)))]
    cursor = 0
    timeline = []
    for ev in events:
        if ev["at"] > cursor:
            timeline.append((cursor, ev["at"] - cursor, None))
        timeline.append((ev["at"], ev["length"], ev))
        cursor = ev["at"] + ev["length"]
    if cursor < len(bars) * BAR:
        timeline.append((cursor, len(bars) * BAR - cursor, None))
    for at, length, ev in timeline:
        parts = []
        while length > 0:
            bar, pos = divmod(at, BAR)
            take = min(length, BAR - pos)
            for v in _pieces(pos, take):
                parts.append((bar, pos, v))
                pos += v
            at += take
            length -= take
        for i, (bar, pos, v) in enumerate(parts):
            tie = "" if ev is None or len(parts) == 1 else "start" if i == 0 else "stop" if i == len(parts) - 1 else "both"
            if bar < len(bars):
                bars[bar].append((pos, v, ev, tie))
    return bars


def _note_xml(pitch: int, value: int, fifths: int, *, chord: bool, tie: str, staff: int = 0,
              string_fret: tuple[int, int] | None = None) -> str:
    step, alter, octave = spell(pitch, fifths)
    kind, dot = TYPES[value]
    ties = "".join(f'<tie type="{t}"/>' for t in (("stop", "start") if tie == "both" else (tie,) if tie else ()))
    tied = "".join(f'<tied type="{t}"/>' for t in (("stop", "start") if tie == "both" else (tie,) if tie else ()))
    tech = f"<technical><string>{string_fret[0]}</string><fret>{string_fret[1]}</fret></technical>" if string_fret else ""
    notations = f"<notations>{tied}{tech}</notations>" if tied or tech else ""
    return ("<note>" + ("<chord/>" if chord else "")
            + f"<pitch><step>{step}</step>" + (f"<alter>{alter}</alter>" if alter else "") + f"<octave>{octave}</octave></pitch>"
            + f"<duration>{value}</duration>{ties}<voice>1</voice><type>{kind}</type>" + ("<dot/>" if dot else "")
            + (f"<staff>{staff}</staff>" if staff else "") + notations + "</note>")


def _rest_xml(value: int, staff: int = 0) -> str:
    kind, dot = TYPES[value]
    return (f"<note><rest/><duration>{value}</duration><voice>1</voice><type>{kind}</type>" + ("<dot/>" if dot else "")
            + (f"<staff>{staff}</staff>" if staff else "") + "</note>")


def _harmony(pitches: list[int], fifths: int) -> str:
    name = chord_name(pitches)
    if not name:
        return ""
    root, kind, _suffix = name
    step, alter, _ = spell(root + 60, fifths)
    return (f"<harmony><root><root-step>{step}</root-step>" + (f"<root-alter>{alter}</root-alter>" if alter else "")
            + f"</root><kind>{kind}</kind></harmony>")


def _staff_xml(bars, fifths: int, *, guitar: bool, staff: int = 0) -> list[str]:
    """Each bar's notes for one staff, as MusicXML."""
    out = []
    last_chord = None
    for items in bars:
        xml = []
        for pos, value, ev, tie in items:
            if ev is None:
                xml.append(_rest_xml(value, staff))
                continue
            if guitar and tie in ("", "start"):
                name = chord_name(ev["pitches"])
                if name and len(set(p % 12 for p in ev["pitches"])) >= 3 and name != last_chord:
                    xml.append(_harmony(ev["pitches"], fifths))
                    last_chord = name
            frets = ev.get("frets") or {}
            for i, p in enumerate(sorted(ev["pitches"])):
                xml.append(_note_xml(p, value, fifths, chord=i > 0, tie=tie, staff=staff,
                                     string_fret=frets.get(p) if guitar else None))
        out.append("".join(xml))
    return out


def musicxml(parts: list[dict], beat: float, title: str, fifths: int) -> str:
    """One score, a part per instrument. `parts`: [{id, name, kind, events,
    total}] with events already on the grid (and fretted, for guitars)."""
    # A whole number on the page -- "121.95" reads as a typo -- and the
    # practice page maps the score's time onto the song's by the ratio of the
    # two (meta's `tempo` and `score_tempo`), so the cursor does not drift.
    tempo = max(1, round(60.0 / beat))
    head = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
            '"http://www.musicxml.org/dtds/partwise.dtd">',
            '<score-partwise version="4.0">',
            f"<work><work-title>{escape(title)}</work-title></work>", "<part-list>"]
    for i, p in enumerate(parts):
        head.append(f'<score-part id="P{i + 1}"><part-name>{escape(p["name"])}</part-name>'
                    f'<score-instrument id="P{i + 1}-I1"><instrument-name>{escape(p["name"])}</instrument-name></score-instrument>'
                    f'<midi-instrument id="P{i + 1}-I1"><midi-channel>{i + 1 if i < 9 else i + 2}</midi-channel>'
                    f'<midi-program>{MIDI_PROGRAM.get(p["id"], 1)}</midi-program></midi-instrument></score-part>')
    head.append("</part-list>")
    total = max((p["total"] for p in parts), default=BAR)
    body = []
    for i, p in enumerate(parts):
        guitar = p["kind"] == "guitar"
        body.append(f'<part id="P{i + 1}">')
        if guitar:
            staves = [_staff_xml(_measures(p["events"], total), fifths, guitar=True)]
        else:
            high = [dict(e, pitches=[x for x in e["pitches"] if x >= 60]) for e in p["events"]]
            low = [dict(e, pitches=[x for x in e["pitches"] if x < 60]) for e in p["events"]]
            staves = [_staff_xml(_measures([e for e in high if e["pitches"]], total), fifths, guitar=False, staff=1),
                      _staff_xml(_measures([e for e in low if e["pitches"]], total), fifths, guitar=False, staff=2)]
        for m in range(len(staves[0])):
            xml = [f'<measure number="{m + 1}">']
            if m == 0:
                if guitar:
                    tuning = "".join(
                        f'<staff-tuning line="{k + 1}"><tuning-step>{spell(pitch, 1)[0]}</tuning-step>'
                        f"<tuning-octave>{spell(pitch, 1)[2]}</tuning-octave></staff-tuning>"
                        for k, pitch in enumerate(TUNING))
                    xml.append(f"<attributes><divisions>{SUB}</divisions><key><fifths>{fifths}</fifths></key>"
                               "<time><beats>4</beats><beat-type>4</beat-type></time>"
                               "<clef><sign>G</sign><line>2</line><clef-octave-change>-1</clef-octave-change></clef>"
                               f"<staff-details><staff-lines>6</staff-lines>{tuning}</staff-details></attributes>")
                else:
                    xml.append(f"<attributes><divisions>{SUB}</divisions><key><fifths>{fifths}</fifths></key>"
                               "<time><beats>4</beats><beat-type>4</beat-type></time><staves>2</staves>"
                               '<clef number="1"><sign>G</sign><line>2</line></clef>'
                               '<clef number="2"><sign>F</sign><line>4</line></clef></attributes>')
                xml.append(f'<direction placement="above"><direction-type><metronome><beat-unit>quarter</beat-unit>'
                           f"<per-minute>{tempo}</per-minute></metronome></direction-type>"
                           f'<sound tempo="{tempo}"/></direction>')
            xml.append(staves[0][m])
            if len(staves) > 1:
                xml.append(f"<backup><duration>{BAR}</duration></backup>")
                xml.append(staves[1][m])
            xml.append("</measure>")
            body.append("".join(xml))
        body.append("</part>")
    return "\n".join(head + body + ["</score-partwise>"])


def build(events: list[dict], tempo_hint: float, title: str) -> tuple[str, dict]:
    """(MusicXML, meta) for MuScriptor's events. The meta carries what the
    practice page needs to keep the score on the song: the beat, the time of
    the score's first bar, and which instruments were written out."""
    notes = [n for n in notes_from_events(events) if n["instrument"] in INSTRUMENTS]
    counts = Counter(n["instrument"] for n in notes)
    keep = [i for i in INSTRUMENTS if counts[i] >= MIN_NOTES]
    notes = [n for n in notes if n["instrument"] in keep]
    if not notes:
        raise ValueError("no guitar or piano was heard in this song")
    beat, first = fit_grid(notes, tempo_hint)
    fifths = key_of(notes)
    parts, lead = [], 0
    for inst in keep:
        mine = [n for n in notes if n["instrument"] == inst]
        evs, lead_i = events_on_grid(mine, beat, first)
        lead = max(lead, lead_i)
        parts.append({"id": inst, "name": GUITARS.get(inst) or PIANOS.get(inst), "kind": "guitar" if inst in GUITARS else "piano",
                      "events": evs})
    # One start for every part: re-grid with the largest pickup any part needs.
    for p in parts:
        p["events"], _ = events_on_grid([n for n in notes if n["instrument"] == p["id"]], beat, first - lead * beat / SUB)
        if p["kind"] == "guitar":
            p["events"] = fret_chords(p["events"])
        p["total"] = max((e["at"] + e["length"] for e in p["events"]), default=BAR)
    start = first - lead * beat / SUB
    xml = musicxml(parts, beat, title, fifths)
    meta = {"tempo": round(60.0 / beat, 3), "score_tempo": max(1, round(60.0 / beat)), "beat": beat,
            "start": round(start, 4), "fifths": fifths,
            "tracks": [{"id": p["id"], "name": p["name"], "kind": p["kind"], "notes": counts[p["id"]]} for p in parts]}
    return xml, meta
