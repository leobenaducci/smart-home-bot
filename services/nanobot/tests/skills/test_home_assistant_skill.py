"""The Home Assistant configuration skill, and the two things it must not do.

`pytest -q tests/skills/test_home_assistant_skill.py` from services/nanobot/.

Home Assistant reaches the assistant through HA's own MCP integration, which
exposes the Assist intents -- `HassTurnOn`, `HassLightSet`, `GetLiveContext`.
Those control devices. None of them can say what an automation triggers on, so
"is this button wired right?" had no skill behind it and the agent answered it
by hand: seventeen numbered probe scripts across the sessions before this one,
a guessed websocket command, and `homeassistant.home` spelled into a file in a
repository whose rule is that names come from `dns:` and nothing invents one.

Two properties are load-bearing and are what this file is really for.

**The config API replaces, it does not patch.** POSTing to
`/api/config/automation/config/<id>` overwrites the whole automation. A config
rebuilt from memory that quietly lost `mode`, or half its `actions`, writes
cleanly and returns 200; it fails days later as "the button stopped working",
which reads like a flat battery. So a proposal that drops a top-level key the
current config has is refused rather than reported.

**The confirmation is in the code, not the prompt.** `set_automation` writes
nothing on the first call: it diffs and returns. A rule in a SKILL.md is
advice, and this repository has already measured what advice is worth -- a
10,798-character standing block saying "not you" did not stop the agent editing
three files itself. A refusal is not advice.
"""

import json
import re
from pathlib import Path

import pytest

from nanobot.agent.runner import _load_skill_python_guide, _static_skill_translation
from nanobot.agent.skills import BUILTIN_SKILLS_DIR

SKILL_DIR = Path(BUILTIN_SKILLS_DIR) / "home-assistant"
SKILL_MD = SKILL_DIR / "SKILL.md"

READS = ["search_ha", "automations_for", "list_automations", "get_automation",
         "get_entity", "describe_device", "list_automation_backups", "list_ha_lights",
         "new_devices", "device_triggers", "listen_button"]
WRITES = ["set_automation", "restore_automation", "enable_automation",
          "disable_automation", "reload_automations",
          "rename_entity", "rename_device", "set_area",
          "pair_zigbee", "identify_device", "create_area",
          "create_automation", "delete_automation"]


@pytest.fixture()
def guide() -> str:
    src = _load_skill_python_guide(str(SKILL_MD))
    assert src, "SKILL_PYTHON.md did not yield a python block"
    return src


@pytest.fixture()
def ns(guide):
    """The guide, executed the way the runner executes it, with HA stubbed out."""
    scope: dict = {}
    exec(compile(guide, "<skill:home-assistant>", "exec"), scope)
    return scope


def test_every_advertised_action_exists(guide):
    """The description is the menu the model orders from.

    An action named there with no `def` behind it does not fail loudly: the
    static translator declines, the LLM fallback rewrites the guide from
    scratch, and what runs is whatever it invented.
    """
    for action in READS + WRITES:
        assert re.search(rf"^def {action}\(", guide, re.MULTILINE), action
        assert action in SKILL_MD.read_text(encoding="utf-8"), \
            f"{action} is implemented but never advertised"


def test_reads_translate_without_an_llm(guide):
    """Static translation is the cheap path; losing it costs a model call each."""
    code = _static_skill_translation(
        {"skill": "home-assistant", "action": "get_automation",
         "automation": "Boton Cama Nico"}, guide)
    assert code and "get_automation(automation='Boton Cama Nico')" in code


def test_it_declares_the_environment_it_needs():
    """Unmet env makes a skill unavailable instead of failing at call time."""
    meta = json.loads(re.search(r"^metadata: (.+)$", SKILL_MD.read_text(encoding="utf-8"),
                                re.MULTILINE).group(1))
    assert set(meta["nanobot"]["requires"]["env"]) == {"HOMEASSISTANT_TOKEN",
                                                       "HOMEASSISTANT_URL"}


def test_no_hostname_is_spelled_anywhere(guide):
    """The address comes from `dns:` through the environment. See CLAUDE.md.

    Every hand-written probe this skill replaces hardcoded `homeassistant.home`,
    which resolves in exactly one household.
    """
    assert ".home" not in guide and "8123" not in guide
    assert 'os.environ.get("HOMEASSISTANT_URL")' in guide


# --- the two refusals -------------------------------------------------------

CURRENT = {"id": "42", "alias": "Boton", "triggers": [{"trigger": "event"}],
           "conditions": [], "actions": [{"action": "light.toggle"}], "mode": "single"}


def _stub(ns, posts):
    def _rest(method, path, body=None):
        if method == "GET" and path == "states":
            return [{"entity_id": "automation.boton", "state": "on",
                     "attributes": {"id": "42", "friendly_name": "Boton"}}]
        if method == "GET" and path.startswith("config/automation/config/"):
            return json.loads(json.dumps(CURRENT))
        posts.append((method, path, body))
        return {"ok": True}
    ns["_rest"] = _rest


def test_a_dropped_top_level_key_is_refused_not_written(ns, tmp_path):
    posts: list = []
    _stub(ns, posts)
    ns["BACKUPS"] = str(tmp_path)
    thin = {k: v for k, v in CURRENT.items() if k != "mode"}

    out = ns["set_automation"]("Boton", thin, confirmed=True)

    assert out["dropped"] == ["mode"]
    assert "error" in out
    assert posts == [], "it refused and wrote anyway"
    # Removing a key can still be the point -- but it has to be said out loud.
    assert "error" not in ns["set_automation"]("Boton", thin, confirmed=True,
                                               allow_removals=True)


def test_the_first_call_diffs_and_writes_nothing(ns, tmp_path):
    posts: list = []
    _stub(ns, posts)
    ns["BACKUPS"] = str(tmp_path)
    changed = json.loads(json.dumps(CURRENT))
    changed["mode"] = "restart"

    out = ns["set_automation"]("Boton", changed)

    assert out["pending"] is True
    assert '-  "mode": "single"' in out["diff"] and '+  "mode": "restart"' in out["diff"]
    assert posts == [], "the unconfirmed call wrote to Home Assistant"

    out = ns["set_automation"]("Boton", changed, confirmed=True)
    assert out["ok"] is True
    assert [p[1] for p in posts] == ["config/automation/config/42",
                                     "services/automation/reload"]
    # The backup is the undo, and it has to hold what was there BEFORE.
    assert json.load(open(out["backup"]))["mode"] == "single"


def test_a_write_that_changes_nothing_is_a_no_op(ns, tmp_path):
    posts: list = []
    _stub(ns, posts)
    ns["BACKUPS"] = str(tmp_path)
    out = ns["set_automation"]("Boton", json.loads(json.dumps(CURRENT)), confirmed=True)
    assert out == {"ok": True, "unchanged": True}
    assert posts == [] and not list(tmp_path.iterdir())


# --- resolving the ids, which is why reading an automation was hard ---------

REGISTRY = {
    "entities": [{"id": "03e5fb77fda6178a431d98b031655750", "entity_id":
                  "button.ts004f_identify_2", "original_name": "Identify",
                  "name": None, "entity_category": "diagnostic",
                  "device_id": "a89728c1062e53a9329d2e6c3585f055", "disabled_by": None}],
    "devices": [{"id": "a89728c1062e53a9329d2e6c3585f055", "name": "TS004F",
                 "name_by_user": "Switch Cama Nico",
                 "manufacturer": "_TZ3000_lrfvzq1e", "model": "TS004F"}],
}


def test_an_opaque_trigger_is_annotated_with_what_it_points_at(ns):
    """The whole finding, in one field.

    `entity_id: 03e5fb77…` and a correct one are the same string to a reader.
    Resolved, this one is ZHA's Identify button -- a diagnostic entity that
    makes the device blink, which nothing on the wall can press.
    """
    ns["_REG"].update(REGISTRY)
    out = ns["_annotate"]({"trigger": "device", "domain": "button", "type": "pressed",
                           "device_id": "a89728c1062e53a9329d2e6c3585f055",
                           "entity_id": "03e5fb77fda6178a431d98b031655750"})

    assert out["_resolved"]["entity_id"] == {
        "entity_id": "button.ts004f_identify_2", "name": "Identify",
        "entity_category": "diagnostic"}
    assert out["_resolved"]["device_id"]["device"] == \
        "Switch Cama Nico (_TZ3000_lrfvzq1e TS004F)"


def test_a_plain_entity_id_is_left_alone(ns):
    """Only the 32-hex registry ids are opaque; `light.nico_lampara` reads fine."""
    ns["_REG"].update(REGISTRY)
    out = ns["_annotate"]({"action": "light.toggle",
                           "target": {"entity_id": "light.nico_lampara"}})
    assert "_resolved" not in out and "_resolved" not in out["target"]


def test_an_id_nothing_owns_says_so_rather_than_vanishing(ns):
    ns["_REG"].update(REGISTRY)
    out = ns["_annotate"]({"device_id": "f" * 32})
    assert "unresolved" in out["_resolved"]["device_id"]


# --- the name people use is the switch, not the automation ------------------

WIRED = {"id": "42", "alias": "Boton Cama Nico",
         "triggers": [{"trigger": "event", "event_type": "zha_event",
                       "event_data": {"device_ieee": "a4:c1:38:12:89:4e:12:b5",
                                      "command": "toggle"}}],
         "actions": [{"action": "light.toggle",
                      "target": {"entity_id": "light.nico_lampara"}}],
         "mode": "single"}

DEVICE_REG = {
    "entities": [{"id": "e" * 32, "entity_id": "switch.ts004f_2", "name": None,
                  "original_name": None, "entity_category": None,
                  "device_id": "d" * 32, "disabled_by": None}],
    "devices": [{"id": "d" * 32, "name": "TS004F", "name_by_user": "Switch Cama Nico",
                 "manufacturer": "_TZ3000_lrfvzq1e", "model": "TS004F",
                 "connections": [["zigbee", "a4:c1:38:12:89:4e:12:b5"]],
                 "identifiers": [["zha", "a4:c1:38:12:89:4e:12:b5"]]}],
}


def _stub_wired(ns):
    def _rest(method, path, body=None):
        if method == "GET" and path == "states":
            return [{"entity_id": "automation.boton_cama_nico", "state": "on",
                     "attributes": {"id": "42", "friendly_name": "Boton Cama Nico"}}]
        return json.loads(json.dumps(WIRED))
    ns["_rest"] = _rest
    ns["_REG"].update(DEVICE_REG)


def test_a_battery_remote_is_found_by_its_radio_address(ns):
    """The only string a zha_event trigger names.

    A battery remote appears in an automation as `device_ieee` and nowhere
    else -- not as a device_id, not as an entity. Matching only on registry ids
    finds nothing and reports a switch that does something as wired to nothing.
    """
    _stub_wired(ns)
    out = ns["automations_for"]("Switch Cama Nico")
    assert [a["alias"] for a in out["automations"]] == ["Boton Cama Nico"]
    assert out["automations"][0]["matched_on"] == ["a4:c1:38:12:89:4e:12:b5"]


def test_the_pair_domain_is_not_part_of_the_search(ns):
    """`("zha", "a4:c1:…")` contributes the address, not the word "zha"."""
    _stub_wired(ns)
    assert "zha" not in ns["_ids_naming"]("Switch Cama Nico")
    assert "zigbee" not in ns["_ids_naming"]("Switch Cama Nico")


def test_naming_the_switch_still_reaches_its_automation(ns):
    """`get_automation("Switch Cama Nico")` -- the device name -- must not dead-end.

    That exact call is what sent the agent back to hand-written probe scripts:
    the device is `Switch Cama Nico`, the automation is `Boton Cama Nico`, and
    "no automation matches" reads like "nothing is wired to it".
    """
    _stub_wired(ns)
    out = ns["get_automation"]("Switch Cama Nico")
    assert out["config"]["alias"] == "Boton Cama Nico"


def test_an_ambiguous_name_asks_instead_of_guessing(ns):
    def _rest(method, path, body=None):
        return [{"entity_id": f"automation.luz_{n}", "state": "on",
                 "attributes": {"id": str(n), "friendly_name": f"Luz {n}"}}
                for n in (1, 2)]
    ns["_rest"] = _rest
    out = ns["_find_automation"]("luz")
    assert out["candidates"] == ["Luz 1", "Luz 2"]


# --- names and areas: registry writes, which REST cannot do -----------------

LIGHT_REG = {
    "entities": [{"id": "e1", "entity_id": "light.living_paula_light_lampara_living",
                  "name": "Paula Light", "original_name": None,
                  "device_id": "d1", "area_id": None, "platform": "smartthings"}],
    "devices": [{"id": "d1", "name": "Paula Light", "name_by_user": None,
                 "manufacturer": "Philips", "model": "A.E27", "area_id": "dormitorio_paula"}],
    "areas": [{"area_id": "dormitorio_paula", "name": "Dormitorio Paula"},
              {"area_id": "living", "name": "Living"}],
}


LISTS = {"config/entity_registry/list": "entities",
         "config/device_registry/list": "devices",
         "config/area_registry/list": "areas"}


@pytest.fixture()
def ws_calls(ns):
    """Stub the websocket: record every command, answer like HA does."""
    import copy
    ns["_REG"].update(copy.deepcopy(LIGHT_REG))
    calls = []

    def fake_ws(*commands):
        out = []
        for cmd, payload in commands:
            calls.append((cmd, payload))
            if cmd == "config/entity_registry/update":
                out.append({"entity_entry": {**LIGHT_REG["entities"][0], **payload}})
            elif cmd == "config/device_registry/update":
                out.append({**LIGHT_REG["devices"][0], **payload})
            elif cmd in LISTS:
                out.append(copy.deepcopy(LIGHT_REG[LISTS[cmd]]))
            else:
                out.append({"error": f"unexpected {cmd}"})
        return out

    ns["_ws"] = fake_ws
    return calls


def test_rename_entity_sends_the_registry_update_and_reports_both_names(ns, ws_calls):
    out = ns["rename_entity"]("Paula Light", "Luz Paula")
    assert ws_calls == [("config/entity_registry/update",
                         {"entity_id": "light.living_paula_light_lampara_living",
                          "name": "Luz Paula"})]
    assert out == {"ok": True, "entity_id": "light.living_paula_light_lampara_living",
                   "was": "Paula Light", "now": "Luz Paula"}


def test_rename_device_writes_name_by_user(ns, ws_calls):
    out = ns["rename_device"]("Paula Light", "Lámpara Paula")
    assert ws_calls == [("config/device_registry/update",
                         {"device_id": "d1", "name_by_user": "Lámpara Paula"})]
    assert out["was"].startswith("Paula Light") and out["now"].startswith("Lámpara Paula")


def test_an_area_ha_does_not_have_is_refused_not_created(ns, ws_calls):
    out = ns["set_area"]("light.living_paula_light_lampara_living", "dormitorio de paula")
    assert "error" in out and "Dormitorio Paula" in out["areas"]
    assert ws_calls == [], "nothing may be written for an unknown area"


def test_set_area_moves_an_entity_by_id_and_a_device_by_name(ns, ws_calls):
    ns["set_area"]("light.living_paula_light_lampara_living", "Living")
    ns["set_area"]("Paula Light", "")
    writes = [c for c in ws_calls if c[0].endswith("/update")]
    assert writes == [
        ("config/entity_registry/update",
         {"entity_id": "light.living_paula_light_lampara_living", "area_id": "living"}),
        ("config/device_registry/update", {"device_id": "d1", "area_id": None}),
    ]


def test_moving_a_device_takes_the_entities_that_were_only_repeating_its_room(ns, ws_calls):
    """Measured on the office light, 2026-09-22.

    Its entity carried `area_id: oficina` of its own, the same as its device.
    Moving the device then moved the device page and left the light -- what
    Assist, the dashboards and room_from_ha read -- in the old room, while the
    call reported success. An entity placed somewhere else on purpose (a relay
    channel in another room) is not touched.
    """
    ns["_REG"]["entities"] = [
        {"id": "e1", "entity_id": "light.a", "device_id": "d1", "area_id": "dormitorio_paula"},
        {"id": "e2", "entity_id": "light.b", "device_id": "d1", "area_id": "living"},
        {"id": "e3", "entity_id": "sensor.c", "device_id": "d1", "area_id": None}]

    out = ns["set_area"]("Paula Light", "Living")

    writes = [c for c in ws_calls if c[0].endswith("/update")]
    assert writes == [
        ("config/device_registry/update", {"device_id": "d1", "area_id": "living"}),
        ("config/entity_registry/update", {"entity_id": "light.a", "area_id": None}),
    ]
    assert out["moved_with_it"] == ["light.a"]
    assert out["kept_their_own_area"] == {"light.b": "Living"}


NEW = {"alias": "Boton oficina", "triggers": [{"trigger": "event", "event_type": "zha_event",
       "event_data": {"device_ieee": "a4:c1:38:00:00:00:00:01", "command": "toggle"}}],
       "actions": [{"action": "light.toggle", "target": {"entity_id": "light.luz_de_oficina"}}]}


def _stub_create(ns, posts, existing=()):
    made = {}

    def _rest(method, path, body=None):
        if method == "GET" and path == "states":
            rows = [{"entity_id": f"automation.{a['id']}", "state": "on",
                     "attributes": {"id": a["id"], "friendly_name": a["alias"]}}
                    for a in list(existing) + list(made.values())]
            return rows
        if method == "GET" and path.startswith("config/automation/config/"):
            aid = path.rsplit("/", 1)[1]
            return json.loads(json.dumps(made.get(aid) or next(a for a in existing if a["id"] == aid)))
        posts.append((method, path, body))
        if method == "POST" and path.startswith("config/automation/config/"):
            made[path.rsplit("/", 1)[1]] = body
        if method == "DELETE":
            made.pop(path.rsplit("/", 1)[1], None)
        return {"ok": True}
    ns["_rest"] = _rest
    ns["time"].sleep = lambda s: None
    return made


def test_create_automation_shows_first_and_writes_only_when_confirmed(ns, tmp_path):
    posts: list = []
    made = _stub_create(ns, posts)

    out = ns["create_automation"](json.loads(json.dumps(NEW)))
    assert out["pending"] is True and out["automation"]["alias"] == "Boton oficina"
    assert posts == [], "the unconfirmed call wrote to Home Assistant"

    out = ns["create_automation"](json.loads(json.dumps(NEW)), confirmed=True)
    assert out["ok"] is True
    assert [p[1] for p in posts] == [f"config/automation/config/{out['id']}",
                                     "services/automation/reload"]
    written = made[out["id"]]
    assert written["mode"] == "single" and written["conditions"] == []


def test_a_second_automation_with_the_same_alias_is_refused(ns):
    posts: list = []
    _stub_create(ns, posts, existing=[{"id": "7", "alias": "boton OFICINA"}])
    out = ns["create_automation"](json.loads(json.dumps(NEW)), confirmed=True)
    assert "already exists" in out["error"] and posts == []


def test_a_deleted_automation_comes_back_from_its_backup(ns, tmp_path):
    """restore_automation used to diff against the current one, which is gone."""
    posts: list = []
    ns["BACKUPS"] = str(tmp_path)
    made = _stub_create(ns, posts, existing=[])
    made["9"] = {"id": "9", **json.loads(json.dumps(NEW)), "mode": "single"}

    assert ns["delete_automation"]("Boton oficina")["pending"] is True
    assert posts == []
    out = ns["delete_automation"]("Boton oficina", confirmed=True)
    assert out["ok"] and "9" not in made
    assert json.load(open(out["backup"]))["alias"] == "Boton oficina"

    assert ns["restore_automation"](out["backup"])["pending"] is True
    assert ns["restore_automation"](out["backup"], confirmed=True)["ok"] is True
    assert made["9"]["alias"] == "Boton oficina"


def test_a_button_with_no_listed_presses_points_at_listen_button(ns, ws_calls):
    """A Tuya TS004F lists only diagnostics: its presses arrive as zha_event.

    Its one `button: pressed` trigger is the Identify button, and wiring that
    is the mistake this skill has already been used to find once.
    """
    listed = [{"platform": "device", "domain": "button", "type": "pressed", "device_id": "d1"},
              {"platform": "device", "domain": "zha", "type": "device_offline",
               "subtype": "device_offline", "device_id": "d1"}]
    ns["_ws"] = lambda *c: [listed]
    out = ns["device_triggers"]("Paula Light")
    assert out["presses"] == [] and "listen_button" in out["note"]

    listed.append({"platform": "device", "domain": "zha", "type": "remote_button_short_press",
                   "subtype": "turn_on", "device_id": "d1", "metadata": {}})
    out = ns["device_triggers"]("Paula Light")
    assert out["presses"] == [{"trigger": "device", "domain": "zha",
                               "type": "remote_button_short_press",
                               "subtype": "turn_on", "device_id": "d1"}]


# --- names, however they are typed -----------------------------------------

NAMED_REG = {
    "entities": [{"id": "e1", "entity_id": "light.luz_de_oficina", "name": "Luz Oficina",
                  "original_name": None, "device_id": "d1", "area_id": None},
                 {"id": "e2", "entity_id": "light.lampara_nico", "name": "Lámpara Nico",
                  "original_name": None, "device_id": "d2", "area_id": None}],
    "devices": [{"id": "d1", "name": "Luz de Oficina", "name_by_user": None},
                {"id": "d2", "name": "Nico Lámpara", "name_by_user": None}],
    "areas": [{"area_id": "dormitorio_paula", "name": "Dormitorio Paula", "aliases": ["pieza pili"]},
              {"area_id": "lavadero", "name": "Logia", "aliases": []}],
}


@pytest.mark.parametrize("typed,expected", [
    ("lampara nico", "light.lampara_nico"),
    ("LÁMPARA  NICO", "light.lampara_nico"),
    ("lámpara-nico", "light.lampara_nico"),
    ("luz de oficina", "light.luz_de_oficina"),      # via the entity id
    ("light.luz_de_oficina", "light.luz_de_oficina"),
])
def test_an_entity_is_found_however_its_name_is_typed(ns, typed, expected):
    import copy
    ns["_REG"].update(copy.deepcopy(NAMED_REG))
    assert ns["_find_entity"](typed)["entity_id"] == expected


@pytest.mark.parametrize("typed,expected", [
    ("nico lampara", "d2"), ("Nico  Lámpara", "d2"), ("luz de oficina", "d1"), ("LUZ-DE-OFICINA", "d1"),
])
def test_a_device_is_found_however_its_name_is_typed(ns, typed, expected):
    import copy
    ns["_REG"].update(copy.deepcopy(NAMED_REG))
    assert ns["_find_device"](typed)["id"] == expected


@pytest.mark.parametrize("typed,area_id", [
    ("dormitorio paula", "dormitorio_paula"), ("Dormitorio-Paula", "dormitorio_paula"),
    ("dormitorio_paula", "dormitorio_paula"), ("Pieza Pili", "dormitorio_paula"),
    ("logía", "lavadero"), ("lavadero", "lavadero"),
])
def test_an_area_is_found_by_name_id_or_alias_without_accents(ns, typed, area_id):
    import copy
    ns["_REG"].update(copy.deepcopy(NAMED_REG))
    calls = []

    def fake_ws(*commands):
        calls.extend(c for c in commands if c[0].endswith("/update"))
        return [copy.deepcopy(NAMED_REG[LISTS[c]]) if c in LISTS else {"entity_entry": {}}
                for c, _ in commands]
    ns["_ws"] = fake_ws
    out = ns["set_area"]("light.lampara_nico", typed)
    assert "error" not in out
    assert calls[0] == ("config/entity_registry/update",
                        {"entity_id": "light.lampara_nico", "area_id": area_id})


def test_create_area_does_not_make_a_second_room_that_differs_by_accents(ns):
    import copy
    ns["_REG"].update(copy.deepcopy(NAMED_REG))
    ns["_ws"] = lambda *c: pytest.fail("it created a duplicate area")
    assert ns["create_area"]("dormitorio  PAULA")["exists"] == "Dormitorio Paula"


def test_an_automation_alias_is_matched_without_accents(ns):
    def _rest(method, path, body=None):
        return [{"entity_id": "automation.boton_cama_nico", "state": "on",
                 "attributes": {"id": "42", "friendly_name": "Botón Cama Nico"}}]
    ns["_rest"] = _rest
    assert ns["_find_automation"]("boton cama nico")["id"] == "42"
    assert ns["create_automation"]({"alias": "BOTON cama  nico", "triggers": [{}],
                                    "actions": [{}]})["error"].endswith("already exists")
