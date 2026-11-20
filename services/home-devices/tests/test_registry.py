"""Discovery: what a scan creates, refreshes, and must never overwrite."""
import json

import pytest

from home_devices.registry import Candidate, Registry


def cand(key, name, area="", kind="light"):
    return Candidate(key=key, backend="ha", name=name, kind=kind, area=area,
                     entities=[{"entity_id": "light." + key.split(":")[-1], "domain": "light", "name": name}])


def test_a_new_candidate_lands_in_the_inbox(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    created = reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi", "Oficina")])
    assert created == ["oficina-tomi"]
    dev = reg.devices["oficina-tomi"]
    assert dev["status"] == "new"
    assert Registry.display(dev)["name"] == "Oficina Tomi"
    assert Registry.display(dev)["room"] == "Oficina"


def test_a_rescan_creates_nothing_and_keeps_what_the_household_typed(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi", "Oficina")])
    reg.update("oficina-tomi", {"name": "Lámpara escritorio", "room": "Estudio", "status": "active"})
    assert reg.observe("ha", [cand("ha:device:a1", "Renamed in HA", "Somewhere")]) == []
    shown = Registry.display(reg.devices["oficina-tomi"])
    assert (shown["name"], shown["room"], shown["status"]) == ("Lámpara escritorio", "Estudio", "active")
    assert reg.devices["oficina-tomi"]["discovered"]["name"] == "Renamed in HA"


def test_a_device_gone_from_a_complete_scan_is_missing_not_deleted(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi"), cand("ha:device:b2", "Luz Paula")])
    reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi")])
    assert reg.devices["luz-paula"]["missing"] is True
    assert reg.devices["oficina-tomi"]["missing"] is False
    reg.observe("ha", [cand("ha:device:b2", "Luz Paula")], complete=False)
    assert reg.devices["luz-paula"]["missing"] is False


def test_an_incremental_scan_says_nothing_about_the_rest(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi"), cand("ha:device:b2", "Luz Paula")])
    reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi")], complete=False)
    assert reg.devices["luz-paula"]["missing"] is False


def test_two_devices_with_one_name_get_two_ids(tmp_path):
    reg = Registry(tmp_path / "devices.json")
    created = reg.observe("ha", [cand("ha:device:a1", "Lámpara"), cand("ha:device:b2", "Lámpara")])
    assert created == ["lampara", "lampara-2"]


def test_it_survives_a_restart(tmp_path):
    path = tmp_path / "devices.json"
    Registry(path).observe("ha", [cand("ha:device:a1", "Oficina Tomi")])
    assert "oficina-tomi" in Registry(path).devices


def test_a_damaged_file_is_refused_not_replaced(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(["not", "a", "registry"]))
    with pytest.raises(ValueError):
        Registry(path)
    assert json.loads(path.read_text()) == ["not", "a", "registry"]


@pytest.mark.parametrize("changes", [{"status": "deleted"}, {"kind": "toaster"}, {"id": "x"},
                                     {"position": {"x": "left"}}, {"aliases": "one"}])
def test_bad_edits_are_refused(tmp_path, changes):
    reg = Registry(tmp_path / "devices.json")
    reg.observe("ha", [cand("ha:device:a1", "Oficina Tomi")])
    with pytest.raises(ValueError):
        reg.update("oficina-tomi", changes)
