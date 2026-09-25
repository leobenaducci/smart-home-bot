"""A list item nobody asked for is refused before it reaches Home Assistant.

10 Sep 2026: asked "¿qué tareas tengo pendientes para hoy?", ornith-1.5:9b
tested whether Home Assistant was answering by adding "test connectivity" and
then "connectivity probe" to the shopping list. The rule refuses the invented
item and nothing else: whatever anyone said, Alfred's earlier replies
included, still goes on the list.
"""

from __future__ import annotations

import pytest

from nanobot.utils.runtime import unrequested_list_item_error

ADD = "mcp_homeassistant_HassListAddItem"


def _convo(*turns):
    roles = ("user", "assistant")
    return [{"role": "system", "content": "prompt with leche and connectivity in it"}] + [
        {"role": roles[i % 2], "content": t} for i, t in enumerate(turns)]


def _refused(item, *turns, tool=ADD):
    return unrequested_list_item_error(tool, {"item": item, "name": "Shopping List"},
                                       _convo(*turns)) is not None


def test_the_reported_probe_items_are_refused():
    ask = "¿qué tareas tengo pendientes para hoy?"
    assert _refused("connectivity probe", ask)
    assert _refused("test connectivity", ask)


def test_the_system_prompt_does_not_count_as_somebody_asking():
    assert _refused("connectivity probe", "¿qué tareas tengo?")


@pytest.mark.parametrize("item, said", [
    ("leche", "agregá leche a la lista"),
    ("Leche", "anotá leche"),
    ("jamón", "falta jamon"),                          # accents fold
    ("huevos", "comprar un huevo"),                    # plural of what was said
    ("tomates", "se acabó el tomate"),
    ("pan de molde", "anotá que falta pan"),           # one word is enough
])
def test_what_the_person_named_goes_on_the_list(item, said):
    assert not _refused(item, said)


def test_items_from_alfreds_own_earlier_reply_are_allowed():
    """"Agregá todo eso" after a recipe: the items are in the reply, not the ask."""
    assert not _refused("harina", "¿qué necesito para panqueques?",
                        "Necesitás harina, huevos y leche.", "agregá todo eso")


def test_text_inside_a_message_with_a_photo_counts():
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:,"}},
        {"type": "text", "text": "agregá lo de la foto: yerba"}]}]
    assert unrequested_list_item_error(ADD, {"item": "yerba"}, messages) is None


@pytest.mark.parametrize("tool", ["mcp_homeassistant_HassTurnOn", "read_file", "exec"])
def test_other_tools_are_not_its_business(tool):
    assert not _refused("connectivity probe", "hola", tool=tool)


def test_a_word_too_short_to_judge_is_let_through():
    assert not _refused("té", "hola")


def test_empty_content_does_not_crash():
    assert unrequested_list_item_error(ADD, {"item": "yerba"},
                                       [{"role": "user", "content": None}]) is not None
