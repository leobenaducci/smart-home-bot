"""A turn that dead-ends says so in the household's language.

The two sentences the runner writes itself -- out of iterations, and stopped
for looping -- were English only, so a Spanish household was told "I reached
the maximum number of tool call iterations" mid-conversation. The language
comes from SEARCH_LANGUAGE, which the manifest fills from `locale.default`.
"""
from nanobot.agent import runner


def test_spanish_household_gets_spanish(monkeypatch):
    monkeypatch.setenv("SEARCH_LANGUAGE", "es")
    text = runner._dead_end("max_iterations", max_iterations=14)
    assert text and "14" in text and "pasos" in text
    assert "iterations" not in text
    assert "acción" in runner._dead_end("loop")


def test_a_regional_tag_still_matches(monkeypatch):
    monkeypatch.setenv("SEARCH_LANGUAGE", "es-CL")
    assert runner._dead_end("loop")


def test_unknown_or_missing_language_keeps_the_english(monkeypatch):
    monkeypatch.setenv("SEARCH_LANGUAGE", "de")
    assert runner._dead_end("max_iterations", max_iterations=3) is None
    monkeypatch.delenv("SEARCH_LANGUAGE", raising=False)
    assert runner._dead_end("loop") is None
