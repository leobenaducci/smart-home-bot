"""A bare skill-invocation block must never be the answer.

Observed in production: asking "¿qué hay de almuerzo mañana?" came back as the
literal text {"skill":"menu","action":"list_menu"}. The interceptor normally
consumes those; when it misses, the block reached the family as the reply.
"""
from nanobot.agent.hook import AgentHookContext
from nanobot.agent.loop import _LoopHook


class _FakeLoop:
    @staticmethod
    def _strip_think(content):
        return (content or "").strip()


def _hook():
    return _LoopHook(_FakeLoop())


def _ctx():
    return AgentHookContext(iteration=0, messages=[])


def test_bare_block_is_dropped():
    out = _hook().finalize_content(_ctx(), '{"skill":"menu","action":"list_menu"}')
    assert out == ""


def test_fenced_bare_block_is_dropped():
    content = '```json\n{"skill":"camera-feed","action":"snapshot","camera":"patio"}\n```'
    assert _hook().finalize_content(_ctx(), content) == ""


def test_block_with_surrounding_prose_is_kept():
    """Someone can legitimately ask to be shown an invocation; that answer has
    prose around it, and a bare block never does."""
    content = 'Para llamarla escribe:\n{"skill":"menu","action":"list_menu"}\ny listo.'
    out = _hook().finalize_content(_ctx(), content)
    assert "Para llamarla" in out
    assert "y listo." in out


def test_ordinary_answer_untouched():
    content = "Mañana hay tallarines con salsa boloñesa."
    assert _hook().finalize_content(_ctx(), content) == content


def test_answer_containing_unrelated_json_is_kept():
    """Not every JSON object is an invocation — a real answer may quote one."""
    content = 'El archivo tiene esto dentro: {"nombre": "Alex", "edad": 40}'
    assert _hook().finalize_content(_ctx(), content) == content


def test_empty_and_none_pass_through():
    assert _hook().finalize_content(_ctx(), "") == ""
    assert _hook().finalize_content(_ctx(), None) == ""
