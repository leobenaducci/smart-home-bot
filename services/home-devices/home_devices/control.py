"""What an action on a device means, per backend. Pure, so it is tested whole.

A device is the household's; an action is what someone asked for (on, off,
brightness 40...). This module turns the pair into one backend call, or into a
refusal that says why -- never into a guess. Asking a sensor to turn on is an
error the assistant can read back, not a silent no-op.
"""
from __future__ import annotations

import unicodedata

ACTIONS = ("on", "off", "toggle", "brightness", "color", "temperature", "position",
           "target_temperature")

# The entity in a device that an action drives, most specific first. Sensors,
# buttons and cameras are not here: they report, they are not switched.
CONTROL_DOMAINS = ("light", "climate", "cover", "fan", "media_player", "vacuum",
                   "humidifier", "water_heater", "valve", "switch", "siren", "lock")

# What a room-wide action touches when nobody named a kind. Lights only: "turn
# off the kitchen" switching off a plug is how a fridge gets turned off.
ROOM_DEFAULT_KINDS = ("light",)

# Never driven from here. Unlocking a door because a model misheard is not a
# mistake to make possible; reading whether it is locked is fine.
REFUSED_DOMAINS = ("lock",)


class ActionError(ValueError):
    pass


def fold(text: str) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower().strip()


def control_entity(device: dict) -> tuple[str, str] | None:
    """(entity_id, domain) of the entity an action drives, or None."""
    ents = [e for r in device.get("routes", []) if r.get("backend") == "ha" and not r.get("missing")
            for e in r.get("entities", [])]
    for domain in CONTROL_DOMAINS:
        for e in ents:
            if e.get("domain") == domain:
                return e["entity_id"], domain
    return None


def capabilities(device: dict) -> list[str]:
    found = control_entity(device)
    if not found:
        return []
    domain = found[1]
    if domain in REFUSED_DOMAINS:
        return []
    caps = ["on", "off", "toggle"]
    if domain == "light":
        caps += ["brightness", "color", "temperature"]
    elif domain == "cover":
        caps += ["position"]
    elif domain == "fan":
        caps += ["brightness"]
    elif domain in ("climate", "water_heater", "humidifier"):
        caps += ["target_temperature"]
    return caps


def _pct(value) -> int:
    try:
        v = round(float(value))
    except (TypeError, ValueError):
        raise ActionError("value must be a number from 0 to 100") from None
    if not 0 <= v <= 100:
        raise ActionError("value must be from 0 to 100")
    return v


def plan(device: dict, action: str, value=None, rgb=None) -> tuple[str, str, str, dict]:
    """(domain, service, entity_id, data) for Home Assistant's call_service."""
    if action not in ACTIONS:
        raise ActionError(f"action is one of {', '.join(ACTIONS)}")
    found = control_entity(device)
    name = device.get("name") or device.get("id")
    if not found:
        raise ActionError(f"{name} cannot be controlled (it only reports)")
    entity, domain = found
    if domain in REFUSED_DOMAINS:
        raise ActionError(f"{name} is a lock; locks are not operated from here")
    if action not in capabilities(device):
        raise ActionError(f"{name} cannot do '{action}'; it can: {', '.join(capabilities(device))}")

    if domain == "cover":
        service = {"on": "open_cover", "off": "close_cover", "toggle": "toggle",
                   "position": "set_cover_position"}[action]
        return domain, service, entity, ({"position": _pct(value)} if action == "position" else {})
    if domain == "vacuum":
        service = {"on": "start", "off": "return_to_base", "toggle": "start_pause"}[action]
        return domain, service, entity, {}
    if domain == "valve":
        service = {"on": "open_valve", "off": "close_valve", "toggle": "toggle"}[action]
        return domain, service, entity, {}
    if action in ("on", "off", "toggle"):
        return domain, {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action], entity, {}
    if action == "brightness":
        pct = _pct(value)
        if domain == "fan":
            return domain, "set_percentage", entity, {"percentage": pct}
        if pct == 0:
            return domain, "turn_off", entity, {}
        return domain, "turn_on", entity, {"brightness_pct": pct}
    if action == "color":
        if not (isinstance(rgb, (list, tuple)) and len(rgb) == 3 and
                all(isinstance(c, (int, float)) and 0 <= c <= 255 for c in rgb)):
            raise ActionError("color needs rgb: three numbers from 0 to 255")
        return domain, "turn_on", entity, {"rgb_color": [int(c) for c in rgb]}
    if action == "temperature":
        try:
            kelvin = int(float(value))
        except (TypeError, ValueError):
            raise ActionError("temperature is a colour temperature in kelvin, 2000-6500") from None
        if not 1500 <= kelvin <= 9000:
            raise ActionError("temperature is a colour temperature in kelvin, 2000-6500")
        return domain, "turn_on", entity, {"color_temp_kelvin": kelvin}
    # target_temperature
    try:
        degrees = float(value)
    except (TypeError, ValueError):
        raise ActionError("target_temperature needs a number of degrees") from None
    if not 5 <= degrees <= 35:
        raise ActionError("target_temperature must be from 5 to 35 degrees")
    return domain, "set_temperature", entity, {"temperature": degrees}


def resolve(devices: list[dict], name: str) -> dict:
    """One device by id, name or alias -- accents and case ignored -- or an
    error listing the candidates. Only devices someone accepted."""
    want = fold(name)
    if not want:
        raise ActionError("name a device")
    active = [d for d in devices if d.get("status") == "active"]
    exact = [d for d in active if want in {fold(d["id"]), fold(d["name"]), *map(fold, d.get("aliases", []))}]
    if len(exact) == 1:
        return exact[0]
    pool = exact or [d for d in active if want in fold(d["name"]) or
                     any(want in fold(a) for a in d.get("aliases", []))]
    if len(pool) == 1:
        return pool[0]
    if not pool:
        raise ActionError(f"no device called '{name}'. Call find to see the names.")
    names = ", ".join(f"{d['name']} ({d['room'] or 'no room'})" for d in pool[:10])
    raise ActionError(f"'{name}' matches several devices: {names}. Use the exact name.")


def in_room(devices: list[dict], room: str, kind: str | None = None) -> list[dict]:
    want = fold(room)
    kinds = (kind,) if kind else ROOM_DEFAULT_KINDS
    rooms = {fold(d["room"]): d["room"] for d in devices if d.get("room")}
    if want not in rooms:
        raise ActionError(f"no room called '{room}'. Rooms: {', '.join(sorted(rooms.values()))}")
    return [d for d in devices if d.get("status") == "active" and fold(d["room"]) == want
            and d.get("kind") in kinds and not d.get("missing")]
