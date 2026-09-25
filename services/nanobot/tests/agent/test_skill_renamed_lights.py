"""The lights skill was called `home-lights` until 2026-09-12.

It is served by the smart-lights plugin, not shipped here, so two things change
together: the row that files it (`SERVICE_SKILL_ENV`) and the text the service
serves. The old name has to keep working across that gap and after it --
stored conversations hold {"skill": "home-lights"} blocks (33 session files for
one member), and a model reading them back writes the old name again.

The old cached copy needs nothing: once no row names `home-lights`, the syncer
drops `.skills-remote/home-lights` as unclaimed.
"""

from __future__ import annotations

from nanobot.agent import remote_skills as rs
from nanobot.agent.runner import _resolve_skill_name
from nanobot.agent.tools.shell import ExecTool

SKILL_PATHS = {"lights": "/ws/.skills-remote/lights/SKILL.md",
               "home-assistant": "/app/nanobot/skills/home-assistant/SKILL.md"}


def test_the_service_is_filed_as_lights():
    found = rs.discover_endpoints({"HOME_LIGHTS_API_URL": "http://hub.home:5010/api"})
    assert found == {"lights": "http://hub.home:5010/api/skill"}


def test_a_block_written_with_the_old_name_runs_lights():
    assert _resolve_skill_name("home-lights", SKILL_PATHS) == "lights"
    assert _resolve_skill_name("home_lights", SKILL_PATHS) == "lights"
    assert _resolve_skill_name("lights", SKILL_PATHS) == "lights"


def test_lights_is_a_name_no_plugin_can_take():
    assert "lights" in rs._shipped_skill_names()
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "lights=IMPOSTOR_URL",
        "IMPOSTOR_URL": "http://impostor.home",
    })
    assert found == {}


def test_curling_the_light_service_is_sent_to_lights():
    error = ExecTool._skill_reimplementation_error(
        'curl -X POST "$HOME_LIGHTS_API_URL/lights/on"')
    assert error and "`lights`" in error
