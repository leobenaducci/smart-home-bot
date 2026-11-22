"""household: the fork's additions to the sub-agent and skills prompts.

Upstream's text stays; the household copies in nanobot/household/templates add
what the fork's said: stop calling tools once the data is in, how a
"Invoke with JSON" skill is used, the report-as-PDF recipe, and an
announcement that passes a finished result and its file link on unchanged.
"""

from __future__ import annotations

import datetime

import pytest

from nanobot.utils.prompt_templates import render_template


@pytest.fixture
def household(monkeypatch):
    monkeypatch.setenv("NANOBOT_HOUSEHOLD", "1")


def test_the_sub_agent_prompt_carries_the_forks_rules_and_the_time(tmp_path, household):
    from agent.conftest import make_provider

    from nanobot.bus.queue import MessageBus
    from nanobot.household.loop import HouseholdAgentLoop

    loop = HouseholdAgentLoop(bus=MessageBus(), provider=make_provider(default_model="cheap"),
                              workspace=tmp_path, model="cheap", context_window_tokens=128_000)
    prompt = loop.subagents._build_subagent_prompt()
    assert "STOP using tools" in prompt
    assert "Invoke with JSON:" in prompt
    assert "md2pdf report.md report.pdf" in prompt
    assert "Current Time: " + datetime.date.today().isoformat() in prompt
    assert "History log:" in prompt                       # upstream's text is kept


def test_the_announcement_passes_a_finished_result_on(household):
    text = render_template("agent/subagent_announce.md", label="flights", status_text="completed",
                           task="Find flights", result="[Report (PDF)](download:media/report.pdf)",
                           stop_reason=None, error=None, partial_result=False)
    assert "Do not carry out the task again" in text
    assert "include that link verbatim" in text


def test_without_the_household_upstream_templates_are_used(monkeypatch):
    monkeypatch.delenv("NANOBOT_HOUSEHOLD", raising=False)
    monkeypatch.setattr("nanobot.household.runtime.RUNTIME.loop", None)
    text = render_template("agent/skills_section.md", skills_summary="x")
    assert "Translatable skills" not in text


def test_md2pdf_renders_a_report(tmp_path):
    from nanobot.household import md2pdf

    src = tmp_path / "report.md"
    src.write_text("# Garden\n\n| plant | water |\n|---|---|\n| roses | Tuesday |\n\n- **Mora** waters\n")
    assert md2pdf.main([str(src), str(tmp_path / "report.pdf")]) == 0
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")
