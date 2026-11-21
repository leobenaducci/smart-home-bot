"""Floor plans: a DXF drawing of the house, read once into what a page draws.

The household draws walls and rooms in any CAD program and saves a DXF --
the one format every one of them writes. What is kept is only what a plan
needs on a phone screen: lines (walls, doors, furniture, all as polylines),
text (room names) and *rooms*: a closed outline with a label inside it. A
device dropped inside a room takes that room's name.

Read here, not in the browser, so the page never ships a DXF parser and the
assistant (phase 5) can ask which room a point is in without one either.

What is left out, on purpose: hatches (fills hide the walls under them),
dimensions (a plan for finding a lamp needs no measurements), anything on a
layer that is off or frozen, and binary DXF -- every program that writes it
also writes ASCII, and the error says so.

Coordinates stay in drawing units, y up, as the CAD program has them. A
device's position is in the same units, so redrawing a room in place leaves
every device where it was.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .registry import slugify

# A house is a few thousand entities. A drawing past these is something else
# (a site survey, a whole building) and would make the page unusable anyway.
MAX_POINTS = 200_000
MAX_TEXTS = 5_000
MAX_INSERT_DEPTH = 8
ARC_STEP = math.radians(10)

UNITS = {0: "", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}

# Entities that become lines.
_DRAWN = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE", "SPLINE", "INSERT",
          "TEXT", "MTEXT", "SOLID", "3DFACE"}


class PlanError(ValueError):
    """A drawing this cannot read, with a reason a person can act on."""


# --- reading the tag stream --------------------------------------------------

def _decode(raw: bytes) -> str:
    if raw.startswith(b"AutoCAD Binary DXF"):
        raise PlanError("binary DXF: save the drawing as ASCII DXF")
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _pairs(text: str):
    lines = text.splitlines()
    if len(lines) < 2:
        raise PlanError("not a DXF drawing")
    for i in range(0, len(lines) - 1, 2):
        code = lines[i].strip()
        try:
            yield int(code), lines[i + 1].strip()
        except ValueError:
            raise PlanError(f"not a DXF drawing (line {i + 1})") from None


def _records(pairs):
    """Group the stream into records: (type, [(code, value), ...]), one per
    code-0 tag, with the section they are in."""
    section, current = None, None
    expect_name = False
    for code, value in pairs:
        if code == 0:
            if current is not None:
                yield section, current
            current = None
            if value == "SECTION":
                expect_name = True
                continue
            if value == "ENDSEC":
                section = None
                continue
            if value == "EOF":
                return
            current = (value, [])
        elif expect_name and code == 2:
            section, expect_name = value, False
            # The header has no code-0 records, only $VARIABLE/value pairs:
            # gather them as one.
            current = (section, []) if section == "HEADER" else None
        elif current is not None:
            current[1].append((code, value))
    if current is not None:
        yield section, current


def _f(tags, code, default=0.0):
    for c, v in tags:
        if c == code:
            try:
                return float(v)
            except ValueError:
                return default
    return default


def _s(tags, code, default=""):
    for c, v in tags:
        if c == code:
            return v
    return default


# --- geometry ----------------------------------------------------------------

def _arc_points(cx, cy, r, a0, a1):
    """Counter-clockwise from a0 to a1 (radians)."""
    while a1 <= a0:
        a1 += 2 * math.pi
    n = max(2, math.ceil((a1 - a0) / ARC_STEP))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n))
            for k in range(n + 1)]


def _bulge_points(p0, p1, bulge):
    """The arc a polyline's bulge draws from p0 to p1, without p0."""
    if not bulge:
        return [p1]
    (x0, y0), (x1, y1) = p0, p1
    chord = math.hypot(x1 - x0, y1 - y0)
    if chord == 0:
        return [p1]
    theta = 4 * math.atan(bulge)                      # signed sweep
    r = chord / (2 * math.sin(abs(theta) / 2))
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    # Distance from the chord's middle to the centre, toward the bulge's side.
    d = r * math.cos(abs(theta) / 2)
    nx, ny = -(y1 - y0) / chord, (x1 - x0) / chord
    # Left of the chord for a counter-clockwise arc; past a half circle `d`
    # turns negative and carries the centre across by itself.
    side = 1 if bulge > 0 else -1
    cx, cy = mx + side * d * nx, my + side * d * ny
    a0 = math.atan2(y0 - cy, x0 - cx)
    n = max(2, math.ceil(abs(theta) / ARC_STEP))
    return [(cx + r * math.cos(a0 + theta * k / n), cy + r * math.sin(a0 + theta * k / n))
            for k in range(1, n + 1)]


def _vertices_to_points(verts, closed):
    """[(x, y, bulge)] -> points, bulges expanded; the closing bulge too."""
    if not verts:
        return []
    pts = [(verts[0][0], verts[0][1])]
    pairs = list(zip(verts, verts[1:])) + ([(verts[-1], verts[0])] if closed else [])
    for a, b in pairs:
        pts += _bulge_points((a[0], a[1]), (b[0], b[1]), a[2])
    if closed and len(pts) > 1 and pts[-1] == pts[0]:
        pts.pop()
    return pts


class _Transform:
    """Block insertion: scale, rotate, translate, composed down the nesting."""

    def __init__(self, m=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)):
        self.m = m  # x' = a*x + b*y + e ; y' = c*x + d*y + f

    def __call__(self, p):
        a, b, c, d, e, f = self.m
        return (a * p[0] + b * p[1] + e, c * p[0] + d * p[1] + f)

    def then(self, outer: "_Transform") -> "_Transform":
        a, b, c, d, e, f = self.m
        A, B, C, D, E, F = outer.m
        return _Transform((A * a + B * c, A * b + B * d, C * a + D * c, C * b + D * d,
                           A * e + B * f + E, C * e + D * f + F))

    @property
    def scale(self):
        a, _b, c, _d, _e, _f = self.m
        return math.hypot(a, c)

    @property
    def angle(self):
        a, _b, c, _d, _e, _f = self.m
        return math.degrees(math.atan2(c, a))

    @staticmethod
    def insert(x, y, sx, sy, rot_deg, bx, by) -> "_Transform":
        r = math.radians(rot_deg)
        cos, sin = math.cos(r), math.sin(r)
        # translate(-base) -> scale -> rotate -> translate(insertion)
        a, b, c, d = cos * sx, -sin * sy, sin * sx, cos * sy
        return _Transform((a, b, c, d, x - (a * bx + b * by), y - (c * bx + d * by)))


# --- text --------------------------------------------------------------------

_MTEXT_CODES = re.compile(r"\\[ACcFfHhQqTtWw][^;]*;|\\[LlOoKk]|\\S[^;]*;|[{}]")


def clean_text(raw: str, mtext: bool) -> str:
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), raw)
    text = text.replace("%%c", "Ø").replace("%%d", "°").replace("%%p", "±").replace("%%%", "%")
    text = re.sub(r"%%[uUoO]", "", text)
    if mtext:
        text = text.replace("\\P", "\n").replace("\\~", " ")
        text = _MTEXT_CODES.sub("", text).replace("\\\\", "\\")
    return text.strip()


# --- the reader --------------------------------------------------------------

def parse_dxf(raw: bytes) -> dict:
    """A DXF drawing as a plan: ``{units, bounds, lines, texts, rooms, layers}``."""
    text = _decode(raw)
    units = ""
    hidden_layers: set[str] = set()
    blocks: dict[str, dict] = {}
    entities: list[tuple[str, list]] = []

    block = None
    header_var = None
    for section, (kind, tags) in _records(_pairs(text)):
        if section == "HEADER":
            # The header is one record of $VARIABLE (code 9) / value pairs.
            for code, value in tags:
                if code == 9:
                    header_var = value
                elif header_var == "$INSUNITS" and code == 70:
                    try:
                        units = UNITS.get(int(value), "")
                    except ValueError:
                        pass
        elif section == "TABLES" and kind == "LAYER":
            name = _s(tags, 2)
            try:
                color, flags = int(_s(tags, 62, "7")), int(_s(tags, 70, "0"))
            except ValueError:
                color, flags = 7, 0
            # A negative colour is "off"; flag 1 is "frozen".
            if color < 0 or flags & 1:
                hidden_layers.add(name)
        elif section == "BLOCKS":
            if kind == "BLOCK":
                block = {"name": _s(tags, 2), "base": (_f(tags, 10), _f(tags, 20)), "entities": []}
            elif kind == "ENDBLK":
                if block:
                    blocks[block["name"]] = block
                block = None
            elif block is not None:
                block["entities"].append((kind, tags))
        elif section == "ENTITIES":
            entities.append((kind, tags))

    out_lines: list[dict] = []
    out_texts: list[dict] = []
    layers: dict[str, int] = {}
    count = [0]

    def layer_index(name):
        if name not in layers:
            layers[name] = len(layers)
        return layers[name]

    def emit(pts, closed, layer):
        if len(pts) < 2:
            return
        count[0] += len(pts)
        if count[0] > MAX_POINTS:
            raise PlanError(f"the drawing is too detailed for a house plan (over {MAX_POINTS} points)")
        out_lines.append({"pts": [[round(x, 3), round(y, 3)] for x, y in pts],
                          "closed": bool(closed), "layer": layer_index(layer)})

    def walk(items, xf: _Transform, depth: int, parent_layer: str):
        i = 0
        while i < len(items):
            kind, tags = items[i]
            i += 1
            layer = _s(tags, 8, "0")
            if layer == "0" and parent_layer:
                layer = parent_layer          # block content on 0 takes the insert's layer
            if layer in hidden_layers or kind not in _DRAWN:
                if kind == "POLYLINE":
                    while i < len(items) and items[i][0] != "SEQEND":
                        i += 1
                    i += 1
                continue
            if kind == "LINE":
                emit([xf((_f(tags, 10), _f(tags, 20))), xf((_f(tags, 11), _f(tags, 21)))], False, layer)
            elif kind == "LWPOLYLINE":
                verts, closed = [], int(_f(tags, 70)) & 1
                for code, value in tags:
                    if code == 10:
                        verts.append([float(value), 0.0, 0.0])
                    elif code == 20 and verts:
                        verts[-1][1] = float(value)
                    elif code == 42 and verts:
                        verts[-1][2] = float(value)
                emit([xf(p) for p in _vertices_to_points(verts, closed)], closed, layer)
            elif kind == "POLYLINE":
                closed, verts = int(_f(tags, 70)) & 1, []
                while i < len(items) and items[i][0] != "SEQEND":
                    vk, vt = items[i]
                    if vk == "VERTEX":
                        verts.append((_f(vt, 10), _f(vt, 20), _f(vt, 42)))
                    i += 1
                i += 1  # SEQEND
                emit([xf(p) for p in _vertices_to_points(verts, closed)], closed, layer)
            elif kind == "CIRCLE":
                pts = _arc_points(_f(tags, 10), _f(tags, 20), _f(tags, 40), 0, 2 * math.pi)[:-1]
                emit([xf(p) for p in pts], True, layer)
            elif kind == "ARC":
                pts = _arc_points(_f(tags, 10), _f(tags, 20), _f(tags, 40),
                                  math.radians(_f(tags, 50)), math.radians(_f(tags, 51)))
                emit([xf(p) for p in pts], False, layer)
            elif kind == "ELLIPSE":
                cx, cy, mx, my = _f(tags, 10), _f(tags, 20), _f(tags, 11), _f(tags, 21)
                ratio, t0, t1 = _f(tags, 40, 1.0), _f(tags, 41, 0.0), _f(tags, 42, 2 * math.pi)
                while t1 <= t0:
                    t1 += 2 * math.pi
                n = max(8, math.ceil((t1 - t0) / ARC_STEP))
                pts = [(cx + mx * math.cos(t) - my * ratio * math.sin(t),
                        cy + my * math.cos(t) + mx * ratio * math.sin(t))
                       for t in (t0 + (t1 - t0) * k / n for k in range(n + 1))]
                emit([xf(p) for p in pts], False, layer)
            elif kind == "SPLINE":
                fit = [(float(v), 0.0) for c, v in tags if c == 11]
                ys = [float(v) for c, v in tags if c == 21]
                if not fit:
                    fit = [(float(v), 0.0) for c, v in tags if c == 10]
                    ys = [float(v) for c, v in tags if c == 20]
                pts = [(x, y) for (x, _), y in zip(fit, ys)]
                emit([xf(p) for p in pts], int(_f(tags, 70)) & 1, layer)
            elif kind in ("SOLID", "3DFACE"):
                pts = [(_f(tags, 10 + k), _f(tags, 20 + k)) for k in range(4)]
                if kind == "SOLID":
                    pts[2], pts[3] = pts[3], pts[2]   # SOLID's corners are in Z order
                emit([xf(p) for p in pts], True, layer)
            elif kind in ("TEXT", "MTEXT"):
                if len(out_texts) >= MAX_TEXTS:
                    continue
                if kind == "MTEXT":
                    raw_text = "".join(v for c, v in tags if c == 3) + _s(tags, 1)
                    pos = (_f(tags, 10), _f(tags, 20))
                    rot = _f(tags, 50)
                    if not rot and any(c == 11 for c, _ in tags):
                        rot = math.degrees(math.atan2(_f(tags, 21), _f(tags, 11)))
                    attach = int(_f(tags, 71, 1)) or 1
                else:
                    raw_text = _s(tags, 1)
                    h, v = int(_f(tags, 72)), int(_f(tags, 73))
                    pos = (_f(tags, 11), _f(tags, 21)) if (h or v) else (_f(tags, 10), _f(tags, 20))
                    rot = _f(tags, 50)
                    # TEXT's justification as MTEXT's attachment point (1-9,
                    # top-left to bottom-right), which is what the page reads.
                    col = {1: 1, 2: 2, 4: 1}.get(h, 0)
                    row = 1 if h == 4 else {3: 0, 2: 1}.get(v, 2)
                    attach = row * 3 + col + 1
                words = clean_text(raw_text, kind == "MTEXT")
                if not words:
                    continue
                x, y = xf(pos)
                out_texts.append({"x": round(x, 3), "y": round(y, 3),
                                  "h": round(_f(tags, 40, 1.0) * xf.scale, 3),
                                  "rot": round(rot + xf.angle, 2), "attach": attach,
                                  "text": words[:200], "layer": layer_index(layer)})
            elif kind == "INSERT":
                name = _s(tags, 2)
                blk = blocks.get(name)
                if not blk or depth >= MAX_INSERT_DEPTH or name.startswith("*"):
                    continue
                inner = _Transform.insert(_f(tags, 10), _f(tags, 20), _f(tags, 41, 1.0), _f(tags, 42, 1.0),
                                          _f(tags, 50), *blk["base"])
                walk(blk["entities"], inner.then(xf), depth + 1, layer)

    walk(entities, _Transform(), 0, "")
    if not out_lines:
        raise PlanError("the drawing has no lines to show")

    xs = [p[0] for ln in out_lines for p in ln["pts"]] + [t["x"] for t in out_texts]
    ys = [p[1] for ln in out_lines for p in ln["pts"]] + [t["y"] for t in out_texts]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    rooms, grid = find_rooms(out_lines, out_texts, bounds)
    return {"units": units, "bounds": bounds, "lines": out_lines, "texts": out_texts,
            "layers": list(layers), "rooms": rooms, "grid": grid}


# --- rooms -------------------------------------------------------------------
#
# Nobody draws a room as a closed outline: walls are lines, a door is a leaf
# and an arc, and the one closed outline in a typical drawing is the whole
# flat. So rooms are *found*, by flood fill. Every line is rasterised onto a
# grid (~600 cells along the long side, ~2 cm in a flat), and every label
# grows outward at once, one cell per step, never crossing a line. A cell
# belongs to whichever label reaches it first:
#
# * walls keep rooms apart, and a door's leaf and swing arc close its opening;
# * an opening with no door (an archway, a kitchen open to the living room)
#   is split where the two waves meet, roughly halfway;
# * a cell no wave reaches -- inside a bed's outline, inside a wall -- takes
#   the nearest room that was reached (`room_at` looks around it).
#
# A label whose region runs off the edge of the drawing and that sits outside
# every closed outline is a note on the sheet ("double glazing"), not a room.

GRID_CELLS = 600
# Lines are thickened by a cell, so walls drawn a hair apart still meet.
_THICK = ((0, 0), (1, 0), (0, 1), (1, 1))
NEAREST_CELLS = 60


def _area(pts) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))) / 2


def contains(pts, x, y) -> bool:
    inside = False
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _room_name(text: str) -> str | None:
    name = text.split("\n")[0].strip()
    if not name or len(name) > 40 or re.fullmatch(r"[\d.,\s×x²m+-]+", name):
        return None                        # a measurement, not a name
    return name


def find_rooms(lines: list[dict], texts: list[dict], bounds: list[float]) -> tuple[list[dict], dict | None]:
    """Rooms (name and label point) and the grid saying which room each cell is
    in, run-length encoded: ``{x0, y0, cell, w, h, runs: [room, count, ...]}``
    with room -1 for a wall or a place no room reached, rows bottom to top."""
    from collections import deque

    labels = [(name, t["x"], t["y"]) for t in texts if (name := _room_name(t["text"]))]
    if not labels:
        return [], None
    x0, y0, x1, y1 = bounds
    cell = max(x1 - x0, y1 - y0) / GRID_CELLS or 1.0
    # A margin of two cells: a wave that leaks out of the house can get round.
    x0, y0 = x0 - 2 * cell, y0 - 2 * cell
    w, h = int((x1 - x0) / cell) + 3, int((y1 - y0) / cell) + 3
    wall = bytearray(w * h)

    def mark(x, y):
        cx, cy = int((x - x0) / cell), int((y - y0) / cell)
        for dx, dy in _THICK:
            if 0 <= cx + dx < w and 0 <= cy + dy < h:
                wall[(cy + dy) * w + cx + dx] = 1

    for ln in lines:
        pts = ln["pts"] + (ln["pts"][:1] if ln["closed"] else [])
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            # Half-cell steps: consecutive marks touch, so a wave cannot slip
            # between them diagonally.
            n = max(1, int(math.hypot(bx - ax, by - ay) / (cell / 2)))
            for k in range(n + 1):
                mark(ax + (bx - ax) * k / n, ay + (by - ay) * k / n)

    owner = [-1] * (w * h)
    queue = deque()
    for idx, (_name, lx, ly) in enumerate(labels):
        cx, cy = int((lx - x0) / cell), int((ly - y0) / cell)
        if 0 <= cx < w and 0 <= cy < h:
            i = cy * w + cx
            if owner[i] == -1:
                owner[i] = idx
                wall[i] = 0               # a label written over a line still counts
                queue.append(i)
    while queue:
        i = queue.popleft()
        o, cx = owner[i], i % w
        for j in (i - w if i >= w else -1, i + w if i + w < w * h else -1,
                  i - 1 if cx > 0 else -1, i + 1 if cx < w - 1 else -1):
            if j >= 0 and owner[j] == -1 and not wall[j]:
                owner[j] = o
                queue.append(j)

    # Notes on the sheet: regions that reach the edge, labelled outside every
    # closed outline.
    edge = set()
    for i in list(range(w)) + list(range((h - 1) * w, h * w)) + \
            list(range(0, w * h, w)) + list(range(w - 1, w * h, w)):
        if owner[i] >= 0:
            edge.add(owner[i])
    outlines = [ln["pts"] for ln in lines if ln["closed"] and len(ln["pts"]) >= 3]
    dropped = {idx for idx in edge if not any(contains(o, labels[idx][1], labels[idx][2]) for o in outlines)}

    # One name may label two regions (a hall labelled twice); they are one room.
    names: list[str] = []
    remap: dict[int, int] = {}
    for idx, (name, _x, _y) in enumerate(labels):
        if idx in dropped:
            remap[idx] = -1
            continue
        if name not in names:
            names.append(name)
        remap[idx] = names.index(name)
    runs: list[int] = []
    prev, count = None, 0
    for o in owner:
        v = remap.get(o, -1)
        if v == prev:
            count += 1
        else:
            if prev is not None:
                runs += [prev, count]
            prev, count = v, 1
    runs += [prev, count]
    rooms = []
    for idx, (name, lx, ly) in enumerate(labels):
        if remap[idx] >= 0 and not any(r["name"] == name for r in rooms):
            rooms.append({"name": name, "x": lx, "y": ly})
    grid = {"x0": round(x0, 4), "y0": round(y0, 4), "cell": cell, "w": w, "h": h, "runs": runs}
    return rooms, grid


def _decode_grid(grid: dict) -> list[int]:
    out: list[int] = []
    runs = grid["runs"]
    for k in range(0, len(runs), 2):
        out += [runs[k]] * runs[k + 1]
    return out


def room_at(plan: dict, x: float, y: float) -> str | None:
    """The room a point on the plan is in, or None off the house."""
    grid = plan.get("grid")
    if not grid:
        return None
    names = [r["name"] for r in plan.get("rooms", [])]
    cells = plan.get("_cells")
    if cells is None:
        cells = plan["_cells"] = _decode_grid(grid)
    w, h = grid["w"], grid["h"]
    cx, cy = int((x - grid["x0"]) / grid["cell"]), int((y - grid["y0"]) / grid["cell"])
    if not (0 <= cx < w and 0 <= cy < h):
        return None
    # Inside furniture or a wall: the nearest cell a room reached, in rings.
    for r in range(NEAREST_CELLS + 1):
        best = None
        for dy in range(-r, r + 1):
            for dx in (range(-r, r + 1) if abs(dy) == r else (-r, r)):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < w and 0 <= ny < h and cells[ny * w + nx] >= 0:
                    d = dx * dx + dy * dy
                    if best is None or d < best[0]:
                        best = (d, cells[ny * w + nx])
        if best:
            return names[best[1]] if best[1] < len(names) else None
    return None


# --- storage -----------------------------------------------------------------

class PlanStore:
    """One JSON file per plan (a floor), plus the DXF it was read from, so a
    better reader later can re-read every plan without asking anyone to
    upload it again."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, plan_id: str, ext: str = "json") -> Path:
        if not re.fullmatch(r"[a-z0-9-]{1,48}", plan_id or ""):
            raise KeyError(plan_id)
        return self.root / f"{plan_id}.{ext}"

    def _write(self, path: Path, data: bytes):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".plan-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")) if self.root.exists() else []:
            try:
                doc = json.loads(p.read_text())
            except ValueError:
                continue
            out.append({k: doc.get(k) for k in ("id", "name", "order", "units", "bounds", "source", "updated")}
                       | {"rooms": [r["name"] for r in doc.get("rooms", [])]})
        out.sort(key=lambda d: (d.get("order") or 0, d.get("name") or ""))
        return out

    def get(self, plan_id: str) -> dict:
        try:
            return json.loads(self._path(plan_id).read_text())
        except FileNotFoundError:
            raise KeyError(plan_id) from None

    def save(self, name: str, raw: bytes, source: str = "", plan_id: str | None = None) -> dict:
        """A new plan, or a new drawing for an existing one (its name and
        order kept). The DXF is read before anything is written."""
        plan = parse_dxf(raw)
        with self._lock:
            if plan_id:
                old = self.get(plan_id)
                name, order = old["name"], old.get("order", 0)
            else:
                name = (name or source.rsplit(".", 1)[0] or "Plan").strip()[:60]
                existing = {p["id"] for p in self.list()}
                base, plan_id, n = slugify(name), slugify(name), 2
                while plan_id in existing:
                    plan_id, n = f"{base}-{n}", n + 1
                order = len(existing)
            doc = {"id": plan_id, "name": name, "order": order, "source": source[:120],
                   "updated": time.time(), **plan}
            self._write(self._path(plan_id, "dxf"), raw)
            self._write(self._path(plan_id), json.dumps(doc, ensure_ascii=False).encode())
            return doc

    def rename(self, plan_id: str, name: str) -> dict:
        with self._lock:
            doc = self.get(plan_id)
            name = str(name or "").strip()[:60]
            if not name:
                raise ValueError("a plan needs a name")
            doc["name"] = name
            self._write(self._path(plan_id), json.dumps(doc, ensure_ascii=False).encode())
            return doc

    def delete(self, plan_id: str) -> None:
        with self._lock:
            self.get(plan_id)
            for ext in ("json", "dxf"):
                try:
                    self._path(plan_id, ext).unlink()
                except FileNotFoundError:
                    pass
