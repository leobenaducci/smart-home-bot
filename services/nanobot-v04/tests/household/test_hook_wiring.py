"""household: the gateway's loop carries the household response hook."""

from nanobot.agent.hook import AgentTurnHookContext
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from nanobot.household import household_hook_factories
from nanobot.household.hooks import HouseholdResponseHook


def test_factories_build_a_response_hook_per_turn(tmp_path):
    tools = ToolRegistry()
    factories = household_hook_factories(Config(), tools)
    hooks = [f(AgentTurnHookContext(workspace=tmp_path, session_key="websocket:x")) for f in factories]
    assert any(isinstance(h, HouseholdResponseHook) for h in hooks)
    a = factories[0](AgentTurnHookContext())
    b = factories[0](AgentTurnHookContext())
    assert a is not b  # per-turn state must not leak between turns


def test_gateway_runtime_installs_the_household_factories():
    import inspect

    import nanobot.cli.gateway_runtime as gr

    assert "household_hook_factories(config, tools)" in inspect.getsource(gr)
