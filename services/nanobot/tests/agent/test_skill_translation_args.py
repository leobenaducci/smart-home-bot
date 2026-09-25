import json
"""The arguments a skill invocation carries have to survive translation.

`{"skill": "grocery", "action": "add_grocery", "name": "yerba"}` was translated
to `add_grocery()`. `name` was on a reserved list — it *is* the skill's own name
in the OpenAI-shaped `{"name": "grocery", ...}` block — and got dropped whatever
it meant. The call then died on a TypeError, the model reread the skill and
wrote the same block again, and asking Alfred to add something to the shopping
list never finished.

The reserved names are also ordinary parameter names, across most of the skills
that write anything down: add_grocery, request_grocery, mark_bought,
remove_grocery, save_place, track_purchase, register_ticket, add_prize. So the
list cannot be applied blind. A reserved key is dropped only when the function
being called does not declare a parameter by that name — and the skill-name
alias never reaches translation at all, because the extractor resolves it into
`skill` first.
"""

import re
from pathlib import Path

import pytest

from nanobot.agent.runner import (
    _guide_signature_params,
    _load_skill_python_guide,
    _static_skill_translation,
    extract_json_skill_invocations,
)
from nanobot.agent.skills import BUILTIN_SKILLS_DIR

SKILLS = Path(BUILTIN_SKILLS_DIR)


def _guide(skill: str) -> str:
    guide = _load_skill_python_guide(str(SKILLS / skill / "SKILL.md"))
    assert guide, f"{skill} has no Python guide to translate against"
    return guide


def _call_line(code: str) -> str:
    """The `print(...)` line the translation appends after the guide."""
    return code.strip().splitlines()[-1]


# --- the reported failure -------------------------------------------------


def test_agrega_yerba_a_la_lista_keeps_the_yerba():
    code = _static_skill_translation(
        {"skill": "grocery", "action": "add_grocery", "name": "yerba"}, _guide("grocery")
    )
    assert code is not None
    assert "add_grocery(name='yerba')" in _call_line(code)


def test_quantity_and_name_both_survive():
    code = _static_skill_translation(
        {"skill": "grocery", "action": "add_grocery", "name": "leche", "qty": "2 L"},
        _guide("grocery"),
    )
    call = _call_line(code)
    assert "name='leche'" in call and "qty='2 L'" in call


def test_mark_bought_names_an_item_rather_than_toggling_nothing():
    # The quiet half of the same bug: mark_bought() is a valid call, so this one
    # posted {"bought": true} with no target instead of raising.
    code = _static_skill_translation(
        {"skill": "grocery", "action": "mark_bought", "name": "pan"}, _guide("grocery")
    )
    assert "mark_bought(name='pan')" in _call_line(code)


# --- the same shape everywhere else --------------------------------------


@pytest.mark.parametrize(
    "skill, action, kwargs, must_keep",
    [
        ("grocery", "request_grocery", {"name": "cereal", "note": "caja azul"}, "name='cereal'"),
        ("grocery", "remove_grocery", {"name": "pan"}, "name='pan'"),
        ("geo", "save_place", {"name": "colegio", "lat": -33.4, "lng": -70.6}, "name='colegio'"),
        ("chores", "add_prize", {"name": "Helado", "cost_points": 50}, "name='Helado'"),
        ("chores", "add_prize",
         {"name": "Helado", "cost_points": 50, "description": "de chocolate"},
         "description='de chocolate'"),
    ],
)
def test_reserved_names_survive_when_the_function_takes_them(skill, action, kwargs, must_keep):
    code = _static_skill_translation({"skill": skill, "action": action, **kwargs}, _guide(skill))
    assert code is not None, f"{skill}.{action} did not translate statically"
    assert must_keep in _call_line(code)


def test_the_invocations_own_keys_are_still_dropped():
    # "skill" and "action" address the invocation, not the function; no guide
    # declares them, so they must not turn into keyword arguments.
    code = _static_skill_translation(
        {"skill": "grocery", "action": "add_grocery", "name": "yerba"}, _guide("grocery")
    )
    call = _call_line(code)
    assert "skill=" not in call and "action=" not in call


def test_a_key_the_function_does_not_declare_is_dropped_only_if_reserved():
    # description is reserved and add_grocery has no such parameter.
    code = _static_skill_translation(
        {"skill": "grocery", "action": "add_grocery", "name": "yerba",
         "description": "la de siempre"},
        _guide("grocery"),
    )
    call = _call_line(code)
    assert "name='yerba'" in call and "description=" not in call


# --- the alias the reserved list existed for ------------------------------


SYSTEM_MESSAGE = {
    "role": "system",
    "content": (
        "## Skills\n"
        f"- **grocery** — lista de compras  `{SKILLS / 'grocery' / 'SKILL.md'}`\n"
    ),
}


def test_openai_shaped_name_is_the_skill_and_does_not_become_an_argument():
    calls = extract_json_skill_invocations(
        '{"name": "grocery", "action": "list_groceries"}',
        [SYSTEM_MESSAGE],
        frozenset({"read_file"}),
    )
    assert len(calls) == 1
    invocation = calls[0].arguments["invocation"]
    assert invocation["skill"] == "grocery"
    assert "name" not in invocation


def test_an_explicit_skill_key_leaves_name_alone():
    calls = extract_json_skill_invocations(
        '{"skill": "grocery", "action": "add_grocery", "name": "yerba"}',
        [SYSTEM_MESSAGE],
        frozenset({"read_file"}),
    )
    assert calls[0].arguments["invocation"]["name"] == "yerba"


# --- the signature reader itself ------------------------------------------


def test_signature_params_reads_defaults_and_stars():
    params = _guide_signature_params("download_file", _guide("file-share"))
    assert {"remote_path", "local_path", "kw"} <= params


def test_signature_params_survives_a_default_containing_a_comma():
    guide = "def f(a, b=(1, 2), c={'x': 1, 'y': 2}):\n    return a"
    assert _guide_signature_params("f", guide) == {"a", "b", "c"}


def test_signature_params_is_empty_for_an_unknown_function():
    assert _guide_signature_params("nope", "def f(a):\n    return a") == frozenset()


def test_every_guide_function_taking_name_can_be_called_with_one():
    """A regression net over the skills, not just the ones listed above.

    Any `def <action>(name...` in a guide is an action a family message can
    reach; each one must translate to a call that still carries the name.
    """
    checked = 0
    for guide_path in SKILLS.glob("*/SKILL_PYTHON.md"):
        guide = _load_skill_python_guide(str(guide_path.parent / "SKILL.md"))
        if not guide:
            continue
        for action in re.findall(r"^def (\w+)\s*\([^)]*\bname\b", guide, re.MULTILINE):
            code = _static_skill_translation(
                {"skill": guide_path.parent.name, "action": action, "name": "x"}, guide
            )
            assert code is not None, f"{guide_path.parent.name}.{action} stopped translating"
            assert "name='x'" in _call_line(code), (
                f"{guide_path.parent.name}.{action} dropped its name argument"
            )
            checked += 1
    assert checked >= 6, f"expected the known name-taking actions, found {checked}"


# --- one argument under the wrong name (2026-09-24) -----------------------

_LIGHTS = ("def _mac(device): return device\n"
           "def turn_on(device): return _mac(device)\n"
           "def set_brightness(device, value): return value\n"
           "def list_lights(): return []\n")


def test_the_light_sent_as_name_is_the_device():
    # qwen3.5:9b, asked to test every light, sent the light as `name`; the
    # reserved key was dropped and turn_on() died seven times.
    code = _static_skill_translation(
        {"skill": "lights", "action": "turn_on", "name": "Oficina Tomi"}, _LIGHTS)
    assert "turn_on(device='Oficina Tomi')" in _call_line(code)


def test_any_single_wrong_name_is_the_device():
    for key in ("light", "mac", "light_name", "id"):
        code = _static_skill_translation(
            {"skill": "lights", "action": "turn_on", key: "B0C1D2E3"}, _LIGHTS)
        assert "turn_on(device='B0C1D2E3')" in _call_line(code), key


def test_nothing_is_guessed_when_two_values_could_be_it():
    code = _static_skill_translation(
        {"skill": "lights", "action": "set_brightness", "light": "x", "level": 50}, _LIGHTS)
    call = _call_line(code)
    assert "device=" not in call and "value=" not in call


def test_a_call_that_needs_nothing_is_left_alone():
    code = _static_skill_translation(
        {"skill": "lights", "action": "list_lights", "name": "x"}, _LIGHTS)
    assert "list_lights()" in _call_line(code)


def test_the_call_written_into_the_action():
    # qwen3.5:9b: {"action": "toggle(Oficina Tomi)"} (2026-09-24).
    for action, want in (("turn_on(Oficina Tomi)", "turn_on(device='Oficina Tomi')"),
                         ("turn_on('B0C1D2E3')", "turn_on(device='B0C1D2E3')"),
                         ("turn_on(Mac:B0C1D2E3)", "turn_on(device='B0C1D2E3')"),
                         ("set_brightness(Oficina, 40)", "set_brightness(device='Oficina', value=40)"),
                         ("set_brightness(value=40, device=Oficina)", "set_brightness(value=40, device='Oficina')"),
                         ("list_lights()", "list_lights()")):
        code = _static_skill_translation({"skill": "lights", "action": action}, _LIGHTS)
        assert code is not None, action
        assert want in _call_line(code), (action, _call_line(code))


def test_a_call_in_the_action_with_too_many_arguments_is_left_alone():
    assert _static_skill_translation(
        {"skill": "lights", "action": "turn_on(a, b)"}, _LIGHTS) is None
    assert _static_skill_translation(
        {"skill": "lights", "action": "nope(a)"}, _LIGHTS) is None


def test_the_history_keeps_the_skill_call_not_its_program():
    # 2026-09-24: the base64 program stayed in history and added ~16k tokens
    # to every later request of the turn.
    from nanobot.agent.runner import _history_tool_calls
    from nanobot.providers.base import LLMResponse, ToolCallRequest
    big = "python -c \"import base64; exec(base64.b64decode(b'" + "QUFB" * 5000 + "'))\""
    r = LLMResponse(content="", tool_calls=[
        ToolCallRequest(id="1", name="exec", arguments={"command": big}),
        ToolCallRequest(id="2", name="exec", arguments={"command": "ls"})])
    r._skills_by_index = {0: "lights"}
    r._skill_invocations = {0: {"action": "turn_on", "device": "Oficina"}}
    calls = _history_tool_calls(r)
    # Named after the skill, as the model thinks of the call: copying it back
    # is a skill call the runner rescues, not a shell comment run as a command.
    assert calls[0]["function"]["name"] == "lights"
    assert json.loads(calls[0]["function"]["arguments"]) == {"action": "turn_on", "device": "Oficina"}
    assert calls[1]["function"]["arguments"] == '{"command": "ls"}'
