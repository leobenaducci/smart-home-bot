"""household: memory, weather and summarize keep the fork's household rules on top of upstream's text."""

from __future__ import annotations

from nanobot.agent.skills import SkillsLoader


def _loader(tmp_path, monkeypatch, on=True):
    if on:
        monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    else:
        monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    return SkillsLoader(tmp_path)


def test_household_versions_shadow_the_builtins(tmp_path, monkeypatch):
    loader = _loader(tmp_path, monkeypatch)
    by_name = {s["name"]: s for s in loader.list_skills(filter_unavailable=False)}
    for name in ("memory", "weather", "summarize"):
        assert by_name[name]["source"] == "household", name
    memory = loader.load_skill("memory")
    assert "session_key" in memory and "History log" in memory
    assert '{"skill": "weather"' in loader.load_skill("weather")
    assert "crawl4ai" in loader.load_skill("summarize")
    assert "memory" in loader.get_always_skills() and "weather" in loader.get_always_skills()


def test_without_the_household_upstream_text_stands(tmp_path, monkeypatch):
    loader = _loader(tmp_path, monkeypatch, on=False)
    by_name = {s["name"]: s for s in loader.list_skills(filter_unavailable=False)}
    assert by_name["memory"]["source"] == "builtin"
    assert "session_key" not in loader.load_skill("memory")
