"""The IR and sensor endpoints, in-process, with no broker and no puck.

Everything the gateway decides on its own is checkable with nothing plugged in
anywhere: what a code may look like, what is stored versus merely sent, what a
listing hides, and which failures are the puck's rather than the request's.
That is what this covers. `ir_smoke.py` is the other half — it needs a wall.

Two behaviours here are worth more than the rest and are easy to break by
accident: trying a code must *not* remember it (test-and-confirm depends on
that), and a puck that never answered must stay distinguishable from one that
answered no.

    docker run --rm -v "$PWD/test:/test:ro" voice-gateway:latest \\
        pytest -q /test/test_ir_endpoints.py

openwakeword is stubbed: it is not on the path being tested, and installing
onnxruntime to check a JSON store would be silly.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import H  # noqa: F401  — the stubs and sys.path live there


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import gateway

    monkeypatch.setattr(gateway, "IR_CODES_FILE", str(tmp_path / "ir-codes.json"))
    monkeypatch.setattr(gateway, "GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(gateway, "_devices", lambda: {"tok-living": "living", "tok-cocina": "cocina"})

    sent = []

    def fake_roundtrip(room, payload, timeout):
        # request_id is minted inside the real _ir_roundtrip, which is what is
        # being stubbed out — so it is not in `payload` here.
        sent.append((room, payload, timeout))
        return {"ok": True, "room": room}

    monkeypatch.setattr(gateway, "_ir_roundtrip", fake_roundtrip)
    monkeypatch.setattr(gateway, "_sensor_snapshot", lambda rooms: {
        "living": {"room": "living", "celsius": 21.4, "humidity": 48, "lux": 96,
                   "motion": False, "seconds_since_motion": 812},
    })
    c = TestClient(gateway.app)
    c.sent = sent
    c.gateway = gateway
    return c




# --- auth -------------------------------------------------------------------

def test_every_ir_route_needs_the_gateway_token(client):
    for method, path, body in [
        ("post", "/v1/ir/send", {"room": "living"}),
        ("post", "/v1/ir/learn", {"room": "living", "device": "d", "command": "c"}),
        ("get", "/v1/ir/codes", None),
        ("post", "/v1/ir/codes", {"room": "living", "device": "d"}),
        ("delete", "/v1/ir/codes", {"room": "living", "device": "d"}),
        ("get", "/v1/sensors", None),
    ]:
        r = client.request(method.upper(), path, json=body)
        assert r.status_code == 401, f"{method} {path} was not gated"


# --- the store --------------------------------------------------------------

def test_registering_an_appliance_normalises_the_protocol(client):
    r = client.post("/v1/ir/codes", headers=H,
                    json={"room": "living", "device": "aire", "brand": "Midea",
                          "ac_protocol": "coolix"})
    assert r.status_code == 200

    got = client.get("/v1/ir/codes?room=living", headers=H).json()
    entry = got["rooms"]["living"]["aire"]
    assert entry["brand"] == "Midea"
    assert entry["ac_protocol"] == "COOLIX"


@pytest.mark.parametrize("payload,shape", [
    ({"protocol": "NEC", "code": "0x20DF10EF", "bits": 32}, "code"),
    ({"protocol": "COOLIX", "state": "B21FC8"}, "state"),
    ({"raw": [1300, 400, 1300, 400], "khz": 38}, "raw"),
])
def test_all_three_code_shapes_round_trip(client, payload, shape):
    r = client.post("/v1/ir/codes", headers=H,
                    json={"room": "living", "device": "tele", "command": "x", **payload})
    assert r.status_code == 200, r.text
    assert r.json()["saved"]["shape"] == shape

    listing = client.get("/v1/ir/codes?room=living", headers=H).json()
    assert listing["rooms"]["living"]["tele"]["commands"]["x"]["shape"] == shape


def test_hex_strings_and_ints_both_work(client):
    """Alfred looks codes up on the web, where they are always hex."""
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "a",
        "protocol": "NEC", "code": "0x20DF10EF", "bits": 32})
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "b",
        "protocol": "NEC", "code": 551489775, "bits": 32})

    raw = json.loads(Path(client.gateway.IR_CODES_FILE).read_text())
    cmds = raw["rooms"]["living"]["devices"]["tele"]["commands"]
    assert cmds["a"]["code"] == cmds["b"]["code"] == 0x20DF10EF


def test_a_listing_hides_raw_timings_but_says_how_many(client):
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "vent", "command": "on",
        "raw": [1300, 400] * 150})

    entry = client.get("/v1/ir/codes?room=living", headers=H).json()
    got = entry["rooms"]["living"]["vent"]["commands"]["on"]
    assert "raw" not in got
    assert got["marks"] == 300


def test_the_store_survives_a_restart(client, tmp_path):
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "encender",
        "protocol": "NEC", "code": "0x1", "bits": 32, "confirmed": True})

    on_disk = json.loads(Path(client.gateway.IR_CODES_FILE).read_text())
    cmd = on_disk["rooms"]["living"]["devices"]["tele"]["commands"]["encender"]
    assert cmd["confirmed"] is True
    assert cmd["source"] == "lookup"
    assert cmd["updated"] > 0


def test_confirmed_defaults_to_false_for_a_looked_up_code(client):
    """Nobody has watched it work yet, and that has to survive to the listing."""
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "c",
        "protocol": "NEC", "code": "0x1", "bits": 32})
    on_disk = json.loads(Path(client.gateway.IR_CODES_FILE).read_text())
    assert on_disk["rooms"]["living"]["devices"]["tele"]["commands"]["c"]["confirmed"] is False

    # …to the listing. The listing is the only place Alfred can read a stored
    # code from, so a `confirmed` that stops at the file is a flag nothing can
    # act on — he states a code nobody has watched work as fact.
    listed = client.get("/v1/ir/codes?room=living", headers=H).json()
    assert listed["rooms"]["living"]["tele"]["commands"]["c"]["confirmed"] is False
    assert listed["rooms"]["living"]["tele"]["commands"]["c"]["source"] == "lookup"


def test_a_confirmed_code_says_so_in_the_listing_too(client):
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "c",
        "protocol": "NEC", "code": "0x1", "bits": 32, "confirmed": True})
    listed = client.get("/v1/ir/codes?room=living", headers=H).json()
    assert listed["rooms"]["living"]["tele"]["commands"]["c"]["confirmed"] is True


def test_forgetting(client):
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "c",
        "protocol": "NEC", "code": "0x1", "bits": 32})

    r = client.request("DELETE", "/v1/ir/codes", headers=H,
                       json={"room": "living", "device": "tele", "command": "c"})
    assert r.status_code == 200
    listing = client.get("/v1/ir/codes?room=living", headers=H).json()
    assert listing["rooms"]["living"]["tele"]["commands"] == {}

    r = client.request("DELETE", "/v1/ir/codes", headers=H,
                       json={"room": "living", "device": "tele"})
    assert r.status_code == 200
    assert client.get("/v1/ir/codes?room=living", headers=H).json()["rooms"]["living"] == {}


# --- bad input --------------------------------------------------------------

def test_unknown_room_is_a_404_that_lists_the_real_ones(client):
    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "garaje", "device": "d", "command": "c"})
    assert r.status_code == 404
    assert "living" in r.json()["detail"]


def test_unknown_command_names_what_the_device_does_know(client):
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "encender",
        "protocol": "NEC", "code": "0x1", "bits": 32})

    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "living", "device": "tele", "command": "apagar"})
    assert r.status_code == 404
    assert "encender" in r.json()["detail"]


@pytest.mark.parametrize("payload", [
    {},                                             # nothing at all
    {"protocol": "NEC"},                            # a protocol and no code
    {"raw": list(range(5000))},                     # not a remote
    {"raw": "not-a-list"},
])
def test_a_code_that_is_not_a_code_is_a_400(client, payload):
    r = client.post("/v1/ir/codes", headers=H,
                    json={"room": "living", "device": "tele", "command": "x", **payload})
    assert r.status_code == 400


def test_an_ac_with_no_protocol_says_how_to_fix_it(client):
    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "living", "device": "aire", "ac": {"degrees": 23}})
    assert r.status_code == 400
    assert "/v1/ir/codes" in r.json()["detail"]


# --- sending ----------------------------------------------------------------

def test_sending_a_saved_command_strips_the_bookkeeping(client):
    """`confirmed`/`source`/`updated` are ours. The puck must not see them."""
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "tele", "command": "encender",
        "protocol": "NEC", "code": "0x20DF10EF", "bits": 32, "confirmed": True})

    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "living", "device": "tele", "command": "encender"})
    assert r.status_code == 200

    room, payload, _ = client.sent[-1]
    assert room == "living"
    assert payload["action"] == "send"
    assert payload["protocol"] == "NEC"
    assert payload["code"] == 0x20DF10EF
    assert "confirmed" not in payload and "source" not in payload and "updated" not in payload


def test_an_adhoc_code_is_sent_but_never_stored(client):
    """Test-and-confirm depends on this: trying a code must not remember it."""
    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "living", "protocol": "NEC", "code": "0x123", "bits": 32})
    assert r.status_code == 200
    assert client.sent[-1][1]["code"] == 0x123
    assert client.get("/v1/ir/codes?room=living", headers=H).json()["rooms"]["living"] == {}


def test_the_ac_protocol_comes_from_the_device_entry(client):
    """So "subilo a 23" does not need Alfred to remember the brand."""
    client.post("/v1/ir/codes", headers=H, json={
        "room": "living", "device": "aire", "ac_protocol": "COOLIX"})

    r = client.post("/v1/ir/send", headers=H, json={
        "room": "living", "device": "aire",
        "ac": {"power": True, "mode": "cool", "degrees": 23}})
    assert r.status_code == 200

    payload = client.sent[-1][1]
    assert payload["ac"]["protocol"] == "COOLIX"
    assert payload["ac"]["degrees"] == 23


def test_a_puck_that_never_answers_is_a_504_not_a_refusal(client, monkeypatch):
    import gateway
    from fastapi import HTTPException

    def silent(room, payload, timeout):
        raise HTTPException(status_code=504, detail=f"the puck in '{room}' did not answer")

    monkeypatch.setattr(gateway, "_ir_roundtrip", silent)
    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "living", "protocol": "NEC", "code": "0x1", "bits": 32})
    assert r.status_code == 504


# --- learning ---------------------------------------------------------------

def test_a_learned_code_is_stored_confirmed(client, monkeypatch):
    import gateway

    monkeypatch.setattr(gateway, "_ir_roundtrip", lambda room, payload, timeout: {
        "ok": True, "protocol": "NEC", "code": 551489775, "bits": 32})

    r = client.post("/v1/ir/learn", headers=H,
                    json={"room": "living", "device": "tele", "command": "silenciar"})
    assert r.status_code == 200
    assert r.json()["learned"] is True

    on_disk = json.loads(Path(gateway.IR_CODES_FILE).read_text())
    cmd = on_disk["rooms"]["living"]["devices"]["tele"]["commands"]["silenciar"]
    # It came off the real remote. There is nothing to confirm.
    assert cmd["confirmed"] is True
    assert cmd["source"] == "learned"


def test_a_learn_that_caught_nothing_stores_nothing(client, monkeypatch):
    import gateway

    monkeypatch.setattr(gateway, "_ir_roundtrip", lambda room, payload, timeout: {
        "ok": False, "detail": "nothing arrived"})

    r = client.post("/v1/ir/learn", headers=H,
                    json={"room": "living", "device": "tele", "command": "x"})
    assert r.status_code == 200
    assert r.json()["learned"] is False
    assert client.get("/v1/ir/codes?room=living", headers=H).json()["rooms"]["living"] == {}


def test_the_gateway_waits_longer_than_the_puck_does(client, monkeypatch):
    """Both timing out at once tells the person the puck is unreachable, which
    is the one thing that did not happen."""
    import gateway
    seen = {}

    def capture(room, payload, timeout):
        seen["puck"] = payload["timeout_s"]
        seen["gateway"] = timeout
        return {"ok": False, "detail": "nothing arrived"}

    monkeypatch.setattr(gateway, "_ir_roundtrip", capture)
    client.post("/v1/ir/learn", headers=H,
                json={"room": "living", "device": "d", "command": "c", "timeout_s": 20})
    assert seen["gateway"] > seen["puck"]


def test_an_unrecognised_remote_is_still_learnable(client, monkeypatch):
    """Raw timings replay perfectly well. Refusing them would mean only famous
    remotes work."""
    import gateway

    monkeypatch.setattr(gateway, "_ir_roundtrip", lambda room, payload, timeout: {
        "ok": True, "raw": [1300, 400, 1300, 400], "khz": 38})

    r = client.post("/v1/ir/learn", headers=H,
                    json={"room": "living", "device": "vent", "command": "on"})
    assert r.json()["learned"] is True
    assert r.json()["shape"] == "raw"


def test_a_capture_too_long_to_be_a_remote_is_refused(client, monkeypatch):
    import gateway

    monkeypatch.setattr(gateway, "_ir_roundtrip", lambda room, payload, timeout: {
        "ok": True, "raw": list(range(5000)), "khz": 38})

    r = client.post("/v1/ir/learn", headers=H,
                    json={"room": "living", "device": "x", "command": "y"})
    assert r.json()["learned"] is False
    assert client.get("/v1/ir/codes?room=living", headers=H).json()["rooms"]["living"] == {}


# --- sensors ----------------------------------------------------------------

def test_sensors_reports_every_room_including_the_silent_ones(client):
    r = client.get("/v1/sensors", headers=H)
    assert r.status_code == 200
    rooms = r.json()["rooms"]

    assert rooms["living"]["sensors"] is True
    assert rooms["living"]["celsius"] == 21.4
    # A room whose puck has no sensors says so rather than reporting zero.
    assert rooms["cocina"] == {"sensors": False}
    assert "celsius" not in rooms["cocina"]


def test_sensors_can_be_narrowed_to_one_room(client):
    r = client.get("/v1/sensors?room=living", headers=H)
    assert set(r.json()["rooms"]) == {"living"}


def test_sensors_for_a_room_that_does_not_exist_is_a_404(client):
    assert client.get("/v1/sensors?room=garaje", headers=H).status_code == 404


# --- what a bad number does -------------------------------------------------

@pytest.mark.parametrize("payload,field", [
    ({"protocol": "NEC", "code": "20DF10EF"}, "code"),      # bare hex, how the web writes them
    ({"protocol": "NEC", "code": "0x1", "bits": "32 bits"}, "bits"),
    ({"raw": [1300, "cuatrocientos"]}, "raw"),
    ({"protocol": "NEC", "code": "0x1", "repeats": "dos"}, "repeats"),
])
def test_a_number_that_is_not_a_number_is_a_400_that_names_the_field(client, payload, field):
    """The caller is a language model. A 500 is the one answer it cannot act on."""
    r = client.post("/v1/ir/codes", headers=H,
                    json={"room": "living", "device": "tele", "command": "x", **payload})
    assert r.status_code == 400, r.text
    assert field in r.json()["detail"]


def test_an_ac_that_is_not_an_object_is_a_400(client):
    """`command` on this same endpoint IS a bare string, so this is the natural
    confusion — and it used to be the only malformed body here that was a 500."""
    r = client.post("/v1/ir/send", headers=H,
                    json={"room": "living", "device": "aire", "ac": "frio 23"})
    assert r.status_code == 400
    assert "object" in r.json()["detail"]


@pytest.mark.parametrize("timeout", ["30s", 30000, 0, -5])
def test_a_learn_timeout_is_bounded(client, timeout):
    """Unbounded it parked a thread and the room's IR receiver for hours — a
    model reading "30 s" as milliseconds sends 30000."""
    r = client.post("/v1/ir/learn", headers=H, json={
        "room": "living", "device": "d", "command": "c", "timeout_s": timeout})
    assert r.status_code == 400, r.text


# --- what the puck says, normalised -----------------------------------------

def test_a_learn_is_normalised_the_same_way_a_lookup_is(client, monkeypatch):
    """The puck's reply used to go to disk verbatim. Two things came through it:
    a hex-string code, which the store then could not summarise — 500ing every
    listing for the whole house, permanently and after the write — and a
    protocol the firmware's own strToDecodeType cannot parse back."""
    import gateway

    monkeypatch.setattr(gateway, "_ir_roundtrip", lambda room, payload, timeout: {
        "ok": True, "protocol": "NEC (Repeat)", "code": "0x20DF10EF", "bits": 32})

    r = client.post("/v1/ir/learn", headers=H,
                    json={"room": "living", "device": "tele", "command": "silenciar"})
    assert r.status_code == 200, r.text
    assert r.json()["learned"] is True

    stored = json.loads(Path(gateway.IR_CODES_FILE).read_text())
    cmd = stored["rooms"]["living"]["devices"]["tele"]["commands"]["silenciar"]
    assert cmd["code"] == 0x20DF10EF          # an int, as every other write path stores
    assert cmd["protocol"] == "NEC"           # sendable: no "(Repeat)" suffix

    # And the listing still answers for the room.
    assert client.get("/v1/ir/codes?room=living", headers=H).status_code == 200


def test_a_capture_that_is_not_a_code_is_refused_rather_than_stored(client, monkeypatch):
    import gateway

    monkeypatch.setattr(gateway, "_ir_roundtrip", lambda room, payload, timeout: {
        "ok": True, "protocol": "NEC", "code": "no-es-un-numero"})

    r = client.post("/v1/ir/learn", headers=H,
                    json={"room": "living", "device": "tele", "command": "x"})
    assert r.status_code == 200
    assert r.json()["learned"] is False
    assert client.get("/v1/ir/codes?room=living", headers=H).json()["rooms"]["living"] == {}


# --- rooms ------------------------------------------------------------------

def test_listing_an_unknown_room_is_a_404_not_an_empty_room(client):
    """200-with-nothing told Alfred the room had no appliances and invited him
    to offer to learn one for a room that cannot exist."""
    r = client.get("/v1/ir/codes?room=garaje", headers=H)
    assert r.status_code == 404
    assert "living" in r.json()["detail"]


def test_forgetting_in_an_unknown_room_is_a_404_that_names_the_rooms(client):
    """The one destructive route was the only one that skipped this."""
    r = client.request("DELETE", "/v1/ir/codes", headers=H,
                       json={"room": "garaje", "device": "tele"})
    assert r.status_code == 404
    assert "living" in r.json()["detail"]


# --- sensors ----------------------------------------------------------------

def test_a_puck_with_no_parts_fitted_says_so(client, monkeypatch):
    """The firmware publishes a retained `{"room": ...}` whether or not any
    sensor is on the board, so keying `sensors` off the message rather than the
    readings answered `true` with nothing to report."""
    import gateway
    monkeypatch.setattr(gateway, "_sensor_snapshot", lambda rooms: {"living": {"room": "living"}})

    rooms = client.get("/v1/sensors", headers=H).json()["rooms"]
    assert rooms["living"] == {"sensors": False}
