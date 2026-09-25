"""Exercise the infrared and sensor endpoints, and be honest about the half
that needs hardware.

Two things live here that look alike and are not. Everything on the gateway
side — the code store, the four ways a request can be malformed, the three
shapes a code can take, what a listing hides — is checkable with nothing
plugged in anywhere, and this asserts all of it. Actually firing a diode at a
television is not, so those steps report UNVERIFIED and do not turn the run
red.

That distinction is the point. A puck that never answers looks exactly like a
puck that answered "no" if you only read the exit code, and the fix for the two
is completely different: one is a wrong code, the other is a thing that is not
plugged in.

By hand:
    python3 ir_smoke.py --base http://compute.home:8083 \
        --gateway-token <gateway token> --room living

    # with a puck on the wall and a remote in your hand:
    python3 ir_smoke.py ... --with-hardware --learn-command prueba
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

PASS, FAIL, SKIP = "PASS", "FAIL", "UNVERIFIED"
_results: list[tuple[str, str, str]] = []


def _record(state: str, name: str, detail: str = "") -> None:
    _results.append((state, name, detail))
    print(f"  {state:10} {name}" + (f" — {detail}" if detail else ""))


def _call(base: str, method: str, path: str, token: str, body: dict | None = None):
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except Exception:
            return e.code, {"detail": raw.decode(errors="replace")}


def check(name: str, got, want) -> bool:
    if got == want:
        _record(PASS, name)
        return True
    _record(FAIL, name, f"expected {want!r}, got {got!r}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8083")
    ap.add_argument("--gateway-token", default="", help="required unless the gateway runs without one")
    ap.add_argument("--room", required=True, help="a room from devices.json")
    ap.add_argument("--device", default="_smoke", help="scratch appliance name; removed at the end")
    ap.add_argument("--with-hardware", action="store_true",
                    help="there is a puck on the wall — run the send and learn steps for real")
    ap.add_argument("--learn-command", default="",
                    help="with --with-hardware: capture a real button under this name")
    args = ap.parse_args()

    tok = args.gateway_token
    ok = True

    print("\nthe code store")
    status, body = _call(args.base, "POST", "/v1/ir/codes", tok, {
        "room": args.room, "device": args.device, "brand": "Smoke", "ac_protocol": "coolix",
    })
    ok &= check("register an appliance", status, 200)

    status, body = _call(args.base, "GET", f"/v1/ir/codes?room={args.room}", tok)
    entry = body.get("rooms", {}).get(args.room, {}).get(args.device, {})
    ok &= check("it comes back", entry.get("brand"), "Smoke")
    # Lowercase in, uppercase out: protocol names are the library's, and a
    # brand typed by a model in a hurry should still match one.
    ok &= check("protocol is normalised", entry.get("ac_protocol"), "COOLIX")

    print("\nthe three shapes a code can take")
    for name, payload, want_shape in [
        ("protocol + code", {"protocol": "NEC", "code": "0x20DF10EF", "bits": 32}, "code"),
        ("protocol + state", {"protocol": "COOLIX", "state": "B21FC8"}, "state"),
        ("raw timings", {"raw": [1300, 400, 1300, 400], "khz": 38}, "raw"),
    ]:
        status, _ = _call(args.base, "POST", "/v1/ir/codes", tok, {
            "room": args.room, "device": args.device, "command": name.replace(" ", "-"), **payload,
        })
        if status != 200:
            ok &= check(f"store {name}", status, 200)
            continue
        _, listing = _call(args.base, "GET", f"/v1/ir/codes?room={args.room}", tok)
        got = listing["rooms"][args.room][args.device]["commands"][name.replace(" ", "-")]
        ok &= check(f"store {name}", got.get("shape"), want_shape)

    # A raw code is hundreds of numbers and a listing is something Alfred asks
    # for just to read the names out.
    _, listing = _call(args.base, "GET", f"/v1/ir/codes?room={args.room}", tok)
    raw_entry = listing["rooms"][args.room][args.device]["commands"]["raw-timings"]
    ok &= check("a listing does not carry raw timings", "raw" in raw_entry, False)
    ok &= check("but says how long it was", raw_entry.get("marks"), 4)

    print("\nthe ways a request can be wrong")
    status, _ = _call(args.base, "POST", "/v1/ir/send", tok,
                      {"room": "no-such-room", "device": "x", "command": "y"})
    ok &= check("unknown room is a 404", status, 404)

    status, body = _call(args.base, "POST", "/v1/ir/send", tok,
                         {"room": args.room, "device": args.device, "command": "no-such-command"})
    ok &= check("unknown command is a 404", status, 404)
    # The caller is a language model. "Not found" on its own invites another guess.
    ok &= check("…and names what does exist", "it knows" in str(body.get("detail", "")), True)

    status, _ = _call(args.base, "POST", "/v1/ir/codes", tok,
                      {"room": args.room, "device": args.device, "command": "junk"})
    ok &= check("a code with no code in it is a 400", status, 400)

    status, _ = _call(args.base, "POST", "/v1/ir/codes", tok, {
        "room": args.room, "device": args.device, "command": "junk",
        "raw": list(range(5000)),
    })
    ok &= check("an implausibly long capture is a 400", status, 400)

    if tok:
        status, _ = _call(args.base, "GET", f"/v1/ir/codes?room={args.room}", "wrong-token")
        ok &= check("a bad token is a 401", status, 401)

    print("\nsensors")
    status, body = _call(args.base, "GET", "/v1/sensors", tok)
    if status != 200:
        ok &= check("the snapshot answers", status, 200)
    else:
        rooms = body.get("rooms", {})
        ok &= check("every room is accounted for", args.room in rooms, True)
        # Retained state, so this answers whether or not a puck is powered —
        # what it must never do is invent a temperature for a room with no
        # sensors in it.
        entry = rooms.get(args.room, {})
        if entry.get("sensors"):
            _record(PASS, "the room reports sensors", json.dumps(entry))
        else:
            _record(PASS, "the room has no sensors, and says so rather than guessing")

    print("\nthe half that needs hardware")
    if not args.with_hardware:
        _record(SKIP, "firing a code", "pass --with-hardware with a puck on the wall")
        _record(SKIP, "learning a code", "pass --with-hardware --learn-command <name>")
    else:
        status, body = _call(args.base, "POST", "/v1/ir/send", tok, {
            "room": args.room, "protocol": "NEC", "code": "0x20DF10EF", "bits": 32,
        })
        if status == 504:
            _record(FAIL, "firing a code", "the puck never answered — powered? on the broker?")
            ok = False
        elif status == 200:
            _record(PASS, "firing a code", f"puck said ok={body.get('ok')}")
        else:
            _record(FAIL, "firing a code", f"{status}: {body}")
            ok = False

        if args.learn_command:
            print("\n  >>> point a remote at the puck and press one button <<<\n")
            status, body = _call(args.base, "POST", "/v1/ir/learn", tok, {
                "room": args.room, "device": args.device, "command": args.learn_command,
            })
            if status == 200 and body.get("learned"):
                _record(PASS, "learning a code", json.dumps(body))
            else:
                _record(FAIL, "learning a code", f"{status}: {body}")
                ok = False
        else:
            _record(SKIP, "learning a code", "no --learn-command")

    # Whatever happened, do not leave a fake television in the house.
    _call(args.base, "DELETE", "/v1/ir/codes", tok, {"room": args.room, "device": args.device})

    failed = [r for r in _results if r[0] == FAIL]
    skipped = [r for r in _results if r[0] == SKIP]
    print(f"\n{len(_results) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} unverified")
    return 0 if ok and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
