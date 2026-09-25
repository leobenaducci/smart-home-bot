"""An attached image becomes a path for describe_image, not a model switch.

The vision model is an 8B running locally with a 4096-token context. Handing it
a whole turn — system prompt plus every tool definition — is 6282 tokens and
fails outright; describe_image succeeds on the same model because it sends a few
hundred tokens and no tools.
"""
from nanobot.agent.loop import _messages_have_image, _swap_images_for_paths


def _img(path=None, url="data:image/png;base64,AAAA"):
    block = {"type": "image_url", "image_url": {"url": url}}
    if path:
        block["_meta"] = {"path": path}
    return block


def test_swaps_an_attached_image_for_its_path():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "resuelve esto"},
        _img("/w/media/api/eq.png"),
    ]}]

    assert _swap_images_for_paths(messages) is True
    assert messages[0]["content"][1] == {
        "type": "text", "text": "[image: /w/media/api/eq.png]"
    }
    assert not _messages_have_image(messages)


def test_swaps_every_image_in_the_turn():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "compara"},
        _img("/w/a.png"), _img("/w/b.png"),
    ]}]

    assert _swap_images_for_paths(messages) is True
    texts = [b["text"] for b in messages[0]["content"] if b["type"] == "text"]
    assert "[image: /w/a.png]" in texts and "[image: /w/b.png]" in texts


def test_refuses_when_any_image_has_no_path():
    """Swapping only some would silently drop a picture — worse than falling
    back to the old routing, which at least still sees it."""
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "mira"},
        _img("/w/a.png"), _img(None),
    ]}]

    assert _swap_images_for_paths(messages) is False
    assert _messages_have_image(messages), "must be left untouched for the fallback"


def test_no_images_is_a_no_op():
    messages = [{"role": "user", "content": "hola"}]
    assert _swap_images_for_paths(messages) is False
    assert messages == [{"role": "user", "content": "hola"}]


def test_leaves_text_blocks_alone():
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "antes"},
        _img("/w/a.png"),
        {"type": "text", "text": "después"},
    ]}]

    _swap_images_for_paths(messages)

    assert [b["text"] for b in messages[0]["content"]] == [
        "antes", "[image: /w/a.png]", "después",
    ]
