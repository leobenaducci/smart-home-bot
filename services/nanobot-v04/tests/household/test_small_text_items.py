"""household A15: skill triggers, the serving model in /status, the history cap."""

from __future__ import annotations

from nanobot.config.schema import ModelPresetConfig
from nanobot.household import outages
from nanobot.household.outage_store import SHARED_OUTAGES
from nanobot.household.skills import with_trigger
from nanobot.providers.base import LLMProvider, LLMResponse


def test_builtin_skills_get_the_forks_use_when_ending(monkeypatch):
    monkeypatch.setattr("nanobot.household.skill_roots.household_enabled", lambda: True)
    assert with_trigger("cron", "Schedule reminders and recurring tasks.").endswith(
        "make something happen on a schedule.")
    tmux = "Use when a CLI task needs an interactive terminal."
    assert with_trigger("tmux", tmux) == tmux                    # already says when
    assert with_trigger("weather", "Forecasts.") == "Forecasts."
    monkeypatch.setattr("nanobot.household.skill_roots.household_enabled", lambda: False)
    assert with_trigger("cron", "Schedule.") == "Schedule."


def test_the_skills_loader_reads_the_trigger(tmp_path, monkeypatch):
    from nanobot.agent.skills import SkillsLoader

    monkeypatch.setattr("nanobot.household.skill_roots.household_enabled", lambda: True)
    description = SkillsLoader(tmp_path).get_skill_description("clawhub")
    assert description.startswith("Search and install") and "Use when asked to find" in description


class _Gw(LLMProvider):
    def __init__(self, base):
        super().__init__(provider_name="custom")
        self.api_base = base

    def get_default_model(self):
        return "cheap"

    async def chat(self, **kwargs):
        return LLMResponse(content="ok", finish_reason="stop")

    async def chat_stream(self, **kwargs):
        return await self.chat(**kwargs)


def test_status_names_the_fallback_while_the_model_is_down(tmp_path):
    SHARED_OUTAGES.use_dir(tmp_path)
    outages._LOCAL.clear()
    primary, backup = _Gw("http://gw.test/v1"), _Gw("http://gw.test/v1")
    wrapper = outages.HouseholdFallbackProvider(
        primary, [ModelPresetConfig(model="flash", provider="custom")], lambda preset: backup)
    assert outages.serving_label(wrapper, "cheap") == "cheap"
    outages.mark_down(outages.gateway_of(primary), "cheap")
    assert outages.serving_label(wrapper, "cheap") == "flash (fallback for cheap)"
    outages._LOCAL.clear()


def test_upstream_keeps_the_history_entry_hard_cap(tmp_path):
    from nanobot.agent import memory

    assert memory._HISTORY_ENTRY_HARD_CAP == 64_000              # the fork's emergency cap


def test_upstream_skills_the_house_does_not_offer_are_hidden(tmp_path, monkeypatch):
    from nanobot.agent.skills import SkillsLoader

    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    names = {s["name"] for s in SkillsLoader(tmp_path).list_skills(filter_unavailable=False)}
    assert "image-generation" not in names and "nanobot-document" not in names
    assert "images" in names and "weather" in names


def test_dead_ends_are_said_in_the_house_language(monkeypatch):
    """Fork runner._DEAD_ENDS: SEARCH_LANGUAGE=es gets the Spanish sentences."""
    from types import SimpleNamespace

    from nanobot.agent.runner import AgentRunner

    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")
    monkeypatch.setenv("SEARCH_LANGUAGE", "es")
    spec = SimpleNamespace(max_iterations_message=None, max_iterations=40)
    assert AgentRunner._max_iterations_fallback(spec).startswith("Llegué al límite de 40 pasos")
    monkeypatch.setenv("SEARCH_LANGUAGE", "en")
    assert "40" in AgentRunner._max_iterations_fallback(spec)
    assert not AgentRunner._max_iterations_fallback(spec).startswith("Llegué")
