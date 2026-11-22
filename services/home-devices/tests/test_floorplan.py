"""Reading a DXF into a plan: walls, labels, rooms, blocks, hidden layers."""
import math

import pytest

from home_devices.floorplan import PlanError, PlanStore, clean_text, parse_dxf, room_at


def dxf(entities: str, blocks: str = "", layers: str = "", units: int = 6) -> bytes:
    """A minimal ASCII DXF. `entities` etc. are code/value lines."""
    def tidy(s):
        return "\n".join(line.strip() for line in s.strip().splitlines() if line.strip())
    parts = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1015", "9", "$INSUNITS", "70", str(units),
             "0", "ENDSEC"]
    if layers:
        parts += ["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", tidy(layers), "0", "ENDTAB",
                  "0", "ENDSEC"]
    if blocks:
        parts += ["0", "SECTION", "2", "BLOCKS", tidy(blocks), "0", "ENDSEC"]
    parts += ["0", "SECTION", "2", "ENTITIES", tidy(entities), "0", "ENDSEC", "0", "EOF"]
    return "\n".join(parts).encode()


def rect(x0, y0, x1, y1, layer="WALLS"):
    return f"""0
LWPOLYLINE
8
{layer}
90
4
70
1
10
{x0}
20
{y0}
10
{x1}
20
{y0}
10
{x1}
20
{y1}
10
{x0}
20
{y1}"""


def label(x, y, text, kind="MTEXT", h=0.3):
    return f"""0
{kind}
8
ROOMS
10
{x}
20
{y}
40
{h}
1
{text}"""


FLAT = "\n".join([
    rect(0, 0, 10, 6),                 # the outline of the whole flat
    rect(0, 0, 4, 6),                  # the kitchen
    rect(4, 0, 10, 6, "WALLS"),        # the living room
    label(2, 3, r"{\fArial|b1;Cocina}"),
    label(7, 3, "Living", kind="TEXT"),
    label(7, 1, "24 m²", kind="TEXT"),  # a measurement, not a room
])


def test_walls_labels_and_units():
    plan = parse_dxf(dxf(FLAT))
    assert plan["units"] == "m"
    assert plan["bounds"] == [0, 0, 10, 6]
    assert len(plan["lines"]) == 3 and all(ln["closed"] for ln in plan["lines"])
    assert [t["text"] for t in plan["texts"]] == ["Cocina", "Living", "24 m²"]


def test_a_room_is_the_smallest_outline_around_a_label():
    plan = parse_dxf(dxf(FLAT))
    assert sorted(r["name"] for r in plan["rooms"]) == ["Cocina", "Living"]
    assert room_at(plan, 1, 1) == "Cocina"
    assert room_at(plan, 9, 5) == "Living"
    assert room_at(plan, 20, 20) is None


def line(x0, y0, x1, y1, layer="WALLS"):
    return f"0\nLINE\n8\n{layer}\n10\n{x0}\n20\n{y0}\n11\n{x1}\n21\n{y1}"


def arc(cx, cy, r, a0, a1):
    return f"0\nARC\n8\nDOORS\n10\n{cx}\n20\n{cy}\n40\n{r}\n50\n{a0}\n51\n{a1}"


# How a flat is really drawn: walls are loose lines, the door is a leaf and a
# swing, the bed is an outline, an archway is a gap, and a note sits outside.
#
#   +---------------+-----------+
#   |               :  arch     |    y 4-5: a gap in the wall
#   |    Living     |Dormitorio |
#   |               /           |    y 2-3: a door, leaf + arc
#   |               |   [bed]   |
#   +---------------+-----------+
#            Escala 1:50
DRAWN = "\n".join([
    line(0, 0, 10, 0), line(10, 0, 10, 6), line(10, 6, 0, 6), line(0, 6, 0, 0),
    line(6, 0, 6, 2), line(6, 3, 6, 4), line(6, 5, 6, 6),
    line(6, 2, 7, 2, "DOORS"), arc(6, 2, 1, 0, 90),
    rect(7.5, 0.5, 9.5, 2.5, "FURN"),
    label(3, 3, "Living"), label(8.5, 4, r"Dormitorio\P12 m²"), label(5, -1, "Escala 1:50", kind="TEXT"),
])


def test_rooms_are_found_between_loose_walls():
    plan = parse_dxf(dxf(DRAWN))
    assert [r["name"] for r in plan["rooms"]] == ["Living", "Dormitorio"]   # the note is not a room
    assert room_at(plan, 1, 1) == "Living"
    assert room_at(plan, 9, 5.5) == "Dormitorio"
    assert room_at(plan, 7.2, 2.8) == "Dormitorio"        # just past the door's swing: the door seals the room
    assert room_at(plan, 4, 4.5) == "Living"               # near the archway, on its own side
    assert room_at(plan, 8.5, 1.5) == "Dormitorio"        # a lamp on the bed
    assert room_at(plan, 5, -1) is None and room_at(plan, 50, 50) is None


def test_a_smaller_label_is_a_landmark_not_a_room():
    plan = parse_dxf(dxf(DRAWN + "\n" + label(0.5, 3, "Ventana", kind="TEXT", h=0.15)))
    assert [r["name"] for r in plan["rooms"]] == ["Living", "Dormitorio"]
    assert room_at(plan, 0.5, 3) == "Living"


def test_a_door_block_is_placed_rotated_and_its_arc_drawn():
    # A door: a leaf line and a quarter-circle swing, drawn at the origin and
    # inserted at (4, 3) turned 90 degrees.
    blocks = """0
BLOCK
8
0
2
DOOR
10
0
20
0
0
LINE
8
0
10
0
20
0
11
1
21
0
0
ARC
8
0
10
0
20
0
40
1
50
0
51
90
0
ENDBLK"""
    insert = """0
INSERT
8
DOORS
2
DOOR
10
4
20
3
50
90"""
    plan = parse_dxf(dxf(insert, blocks=blocks))
    leaf, swing = plan["lines"]
    assert leaf["pts"] == [[4, 3], [4, 4]]
    assert plan["layers"][leaf["layer"]] == "DOORS"          # block content on 0 takes the insert's layer
    end = swing["pts"][-1]
    assert math.isclose(end[0], 3, abs_tol=1e-3) and math.isclose(end[1], 3, abs_tol=1e-3)


def test_a_bulge_draws_a_half_circle_on_the_right_side():
    poly = """0
LWPOLYLINE
8
W
90
2
70
0
10
0
20
0
42
1
10
2
20
0"""
    pts = parse_dxf(dxf(poly))["lines"][0]["pts"]
    assert pts[0] == [0, 0] and pts[-1] == [2, 0]
    assert min(p[1] for p in pts) == pytest.approx(-1, abs=1e-3)   # counter-clockwise passes below


def test_a_layer_that_is_off_is_left_out():
    layers = """0
LAYER
2
FURNITURE
70
0
62
-7"""
    plan = parse_dxf(dxf(FLAT + "\n" + rect(1, 1, 2, 2, "FURNITURE"), layers=layers))
    assert "FURNITURE" not in plan["layers"]


def test_mtext_formatting_is_stripped():
    assert clean_text(r"{\fArial|b0|i0;\C1;Ba\U+00F1o}\Pde visitas", True) == "Baño\nde visitas"
    assert clean_text("%%c50", False) == "Ø50"


def test_what_cannot_be_read_says_why():
    with pytest.raises(PlanError, match="ASCII"):
        parse_dxf(b"AutoCAD Binary DXF\r\n\x1a\x00...")
    with pytest.raises(PlanError, match="no lines"):
        parse_dxf(dxf(label(0, 0, "Solo texto")))
    with pytest.raises(PlanError):
        parse_dxf(b"hello")


def test_the_store_keeps_name_and_order_across_a_new_drawing(tmp_path):
    store = PlanStore(tmp_path)
    first = store.save("Planta baja", dxf(FLAT), source="casa.dxf")
    second = store.save("", dxf(FLAT), source="arriba.dxf")
    assert (first["id"], second["id"]) == ("planta-baja", "arriba")
    store.rename("planta-baja", "PB")
    again = store.save("ignored", dxf(rect(0, 0, 1, 1)), plan_id="planta-baja")
    assert again["name"] == "PB" and again["order"] == 0 and again["rooms"] == []
    assert [p["id"] for p in store.list()] == ["planta-baja", "arriba"]
    assert (tmp_path / "planta-baja.dxf").exists()
    store.delete("arriba")
    assert [p["id"] for p in store.list()] == ["planta-baja"]
    with pytest.raises(KeyError):
        store.get("../etc")
