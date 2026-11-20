"""What an action means: one backend call, or a refusal that says why."""
import pytest

from home_devices import control
from home_devices.control import ActionError, plan


def device(name, *entities, status="active", room="Oficina", kind="light", aliases=()):
    return {"id": control.fold(name).replace(" ", "-"), "name": name, "room": room, "kind": kind,
            "status": status, "aliases": list(aliases),
            "routes": [{"backend": "ha", "key": "ha:device:x",
                        "entities": [{"entity_id": e, "domain": e.split(".")[0]} for e in entities]}]}


LAMP = device("Oficina Tomi", "sensor.oficina_tomi_power", "light.oficina_tomi")


def test_on_off_drive_the_light_not_the_sensor():
    assert plan(LAMP, "on") == ("light", "turn_on", "light.oficina_tomi", {})
    assert plan(LAMP, "off")[1] == "turn_off"


def test_brightness_colour_and_temperature():
    assert plan(LAMP, "brightness", 40)[3] == {"brightness_pct": 40}
    assert plan(LAMP, "brightness", 0)[1] == "turn_off"
    assert plan(LAMP, "color", rgb=[255, 0, 0])[3] == {"rgb_color": [255, 0, 0]}
    assert plan(LAMP, "temperature", 2700)[3] == {"color_temp_kelvin": 2700}


@pytest.mark.parametrize("action,value,rgb", [("brightness", 140, None), ("brightness", "half", None),
                                              ("color", None, [1, 2]), ("temperature", 100, None),
                                              ("dance", None, None)])
def test_bad_values_are_refused(action, value, rgb):
    with pytest.raises(ActionError):
        plan(LAMP, action, value, rgb)


def test_a_sensor_only_reports():
    with pytest.raises(ActionError, match="only reports"):
        plan(device("Puerta", "binary_sensor.puerta", kind="binary_sensor"), "on")


def test_a_lock_is_never_operated():
    with pytest.raises(ActionError, match="lock"):
        plan(device("Puerta calle", "lock.puerta", kind="lock"), "off")


def test_a_cover_opens_and_moves():
    cover = device("Cortina", "cover.cortina", kind="cover")
    assert plan(cover, "on")[1] == "open_cover"
    assert plan(cover, "position", 30)[3] == {"position": 30}


def test_a_switch_cannot_dim():
    with pytest.raises(ActionError, match="cannot do 'brightness'"):
        plan(device("Enchufe", "switch.enchufe", kind="switch"), "brightness", 50)


def test_resolve_by_name_alias_and_accents_and_only_accepted_ones():
    devs = [device("Lámpara Paula", "light.a", aliases=["la de Paula"]),
            device("Lámpara Tomi", "light.b"),
            device("Lámpara nueva", "light.c", status="new")]
    assert control.resolve(devs, "lampara paula")["name"] == "Lámpara Paula"
    assert control.resolve(devs, "LA DE PAULA")["name"] == "Lámpara Paula"
    with pytest.raises(ActionError, match="several"):
        control.resolve(devs, "lampara")
    with pytest.raises(ActionError, match="no device"):
        control.resolve(devs, "lampara nueva")


def test_a_room_means_its_lights_unless_a_kind_is_named():
    devs = [device("Luz Paula", "light.a", room="Living"),
            device("Enchufe tele", "switch.t", room="Living", kind="switch")]
    assert [d["name"] for d in control.in_room(devs, "living")] == ["Luz Paula"]
    assert [d["name"] for d in control.in_room(devs, "Living", "switch")] == ["Enchufe tele"]
    with pytest.raises(ActionError, match="Rooms: Living"):
        control.in_room(devs, "Cocina")
