"""Trial port: household prompt files reach the system prompt, in order."""

from nanobot.agent.context import ContextBuilder


def test_family_and_tools_are_loaded_between_soul_and_user(tmp_path):
    for name, body in [("AGENTS.md", "agents rules"), ("SOUL.md", "soul text"),
                       ("FAMILY.md", "family rules"), ("TOOLS.md", "tool notes"),
                       ("USER.md", "user profile")]:
        (tmp_path / name).write_text(body, encoding="utf-8")
    prompt = ContextBuilder(tmp_path).build_system_prompt()
    order = [prompt.find(t) for t in ("soul text", "family rules", "tool notes", "user profile")]
    assert all(i >= 0 for i in order), order
    assert order == sorted(order)
