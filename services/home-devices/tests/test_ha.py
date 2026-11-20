"""Home Assistant's registries, as candidates."""
from home_devices.ha import candidates_from

AREAS = [{"area_id": "oficina", "name": "Oficina"}, {"area_id": "living", "name": "Living"}]
DEVICES = [
    {"id": "d-plug", "name": "TS011F", "name_by_user": "Enchufe Tomi", "area_id": "oficina",
     "manufacturer": "_TZ3000", "model": "TS011F"},
    {"id": "d-bulb", "name": "Luz Paula", "area_id": None},
    {"id": "d-sun", "name": "Sun", "entry_type": "service"},
    {"id": "d-diag", "name": "Coordinator"},
]
ENTITIES = [
    {"entity_id": "switch.enchufe_tomi", "device_id": "d-plug"},
    {"entity_id": "sensor.enchufe_tomi_power", "device_id": "d-plug", "original_name": "Power"},
    {"entity_id": "sensor.enchufe_tomi_lqi", "device_id": "d-plug", "entity_category": "diagnostic"},
    {"entity_id": "light.luz_paula", "device_id": "d-bulb", "area_id": "living"},
    {"entity_id": "sensor.sun_next_dawn", "device_id": "d-sun"},
    {"entity_id": "sensor.coordinator_rssi", "device_id": "d-diag", "entity_category": "diagnostic"},
    {"entity_id": "light.old_lamp", "device_id": "d-bulb", "disabled_by": "user"},
    {"entity_id": "automation.morning"},
    {"entity_id": "binary_sensor.puerta", "area_id": "living"},
]
STATES = [{"entity_id": "binary_sensor.puerta", "attributes": {"friendly_name": "Puerta"}}]


def by_key():
    return {c.key: c for c in candidates_from(AREAS, DEVICES, ENTITIES, STATES)}


def test_entities_group_under_their_device_and_plumbing_is_left_out():
    c = by_key()
    assert set(c) == {"ha:device:d-plug", "ha:device:d-bulb", "ha:entity:binary_sensor.puerta"}
    plug = c["ha:device:d-plug"]
    assert [e["entity_id"] for e in plug.entities] == ["sensor.enchufe_tomi_power", "switch.enchufe_tomi"]


def test_the_kind_is_the_most_specific_domain_and_the_name_is_the_users():
    plug = by_key()["ha:device:d-plug"]
    assert (plug.kind, plug.name, plug.area, plug.model) == ("switch", "Enchufe Tomi", "Oficina", "TS011F")


def test_the_area_falls_back_to_an_entitys():
    assert by_key()["ha:device:d-bulb"].area == "Living"


def test_a_loose_entity_is_a_candidate_of_its_own():
    door = by_key()["ha:entity:binary_sensor.puerta"]
    assert (door.name, door.kind, door.area) == ("Puerta", "binary_sensor", "Living")
