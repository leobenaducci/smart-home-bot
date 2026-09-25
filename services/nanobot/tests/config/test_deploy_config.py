"""The deployed config is one file, and it has to load.

This house ran two configs for months: `config/config.json`, mounted into
every container, and a second one at the repo root that the Dockerfile baked to
`/etc/nanobot/config.json` as the fallback. They drifted — the mounted one moved
to deepseek while the baked one still said qwen on Together, carried an empty
WebSocket token (which reads as "gate off") and a stale SSRF whitelist. Nothing
compared them, and the difference only surfaced where nobody was looking: a
container that came up without its mount.

There is one file now. These tests are what keeps it that way, and what keeps a
config that cannot load from reaching a Sunday deploy: `Config` forbids unknown
keys at the top level, so a `_comment` written one level too high is not a typo
in a comment — it is five family members with no assistant until someone reads
the container log.
"""
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEPLOY_CONFIG = REPO / "config" / "config.json"


def test_there_is_exactly_one_config_in_the_repo():
    assert DEPLOY_CONFIG.is_file()
    assert not (REPO / "config.json").exists(), (
        "a second config at the repo root is what drifted last time — "
        "config/config.json is the one that ships")


def test_the_image_bakes_the_file_the_mount_provides():
    """Otherwise the fallback is a different assistant, not an older one."""
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "COPY --chown=nanobot:nanobot config/config.json /etc/nanobot/config.json" \
        in dockerfile


def test_the_search_rules_name_tools_that_exist(cfg):
    """SOUL.md tells Alfred which search to reach for, and names them.

    A rule about a tool that is not called that is not a rule — it is a
    paragraph he cannot act on, and nothing fails when it drifts. Two ways it
    drifts: the built-in tool is renamed, or Bright Data's `enabledTools` is
    narrowed and the tool the rule recommends stops being there.

    MCP tools are exposed as `mcp_<server>_<tool>` (agent/tools/mcp.py), not by
    their bare name — which is what the first draft of this rule got wrong.
    """
    from nanobot.agent.tools.web import WebFetchTool, WebSearchTool

    soul = (REPO / "config" / "SOUL.md").read_text(encoding="utf-8")
    section = soul.split("## Web Search", 1)
    assert len(section) == 2, "the Web Search rules are gone from SOUL.md"
    section = section[1].split("\n## ", 1)[0]

    for tool in (WebSearchTool.name, WebFetchTool.name):
        assert tool in section, f"the rules do not mention {tool}"

    raw = json.loads(DEPLOY_CONFIG.read_text())
    brightdata = raw["tools"]["mcpServers"]["brightdata"]
    enabled = brightdata.get("enabledTools") or []
    for named in re.findall(r"mcp_brightdata_(\w+)", section):
        assert named in enabled, (
            f"SOUL.md sends Alfred to mcp_brightdata_{named}, which is not in "
            f"this deployment's enabledTools: {enabled}")

    # The point of the rules is that one of them is free and one is not, so the
    # paid one has to be marked as paid or the whole distinction is decorative.
    assert "costs money" in section.lower()


def test_alfreds_own_prompt_does_not_name_a_model_or_a_provider(cfg):
    """`config/AGENTS.md` describes the roster by role, and names nothing.

    This test used to assert the opposite -- that every model the deployment
    runs was named in the Model Configuration block -- and its own docstring
    recorded why that was a losing game: on 2026-08-16 the roster moved and
    Alfred went on introducing himself as the old model, because the config had
    changed and the prompt had not. Forcing them to match makes the prompt a
    second copy of the configuration, and a copy is a thing that drifts.

    It drifted again, worse: by 2026-08-31 the block named a gateway the house
    no longer talked to and five models it no longer ran, and every turn
    carried that as a confident statement about itself.

    So the duplication is gone. The model serving a turn reaches the prompt
    from the runtime -- `Model:` in the Runtime Context block, written per turn
    by agent/context.py -- which cannot drift because it is generated from the
    call being made. The prompt describes what each role is *for*, and this
    test keeps it that way.

    No provider is privileged either: a role may name any of them, several can
    be live at once, and a prompt that says otherwise is wrong for every
    household that moved.
    """
    prompt = (REPO / "config" / "AGENTS.md").read_text(encoding="utf-8")
    block = prompt.split("## Model Configuration", 1)
    assert len(block) == 2, "the Model Configuration section is gone"
    block = block[1].split("\n## ", 1)[0]

    d = cfg.agents.defaults
    live = {d.model, d.subagent_model, d.subagent_model_powerful,
            d.model_powerful, d.model_fallback}
    live |= set(d.model_profiles.values())
    live.discard(None)
    named = sorted(m for m in live if isinstance(m, str) and m and m in block)
    assert not named, (
        f"the prompt names {named}. Which model serves a role is the "
        f"household's configuration and the runtime states it per turn; a copy "
        f"here is wrong the first time somebody changes one."
    )

    # The roles themselves still have to be described -- removing the names is
    # not licence to remove the meaning.
    lowered = block.lower()
    for role in ("main agent", "sub-agent", "powerful", "vision", "fallback"):
        assert role in lowered, f"the block no longer explains {role!r}"

    # And no provider may be presented as the place models come from.
    for vendor in ("opencode", "zen/go", "zen/v1", "together.ai",
                   "api.together", "openrouter.ai"):
        assert vendor not in lowered, (
            f"{vendor!r} is named as the provider. They are peers -- a role "
            f"picks one with a prefix and several can be live at once."
        )
def test_the_voice_config_loads_too():
    """`config/instances/house/config.json` is the second deployed config in
    this repo and had no check at all — the same `_comment`-at-the-top-level
    mistake that takes the members down takes the kitchen down, and it is a
    separate unit in the manifest, so nothing else would catch it first.

    It is deliberately NOT an overlay: it holds fewer credentials than the base
    (no brightdata, nothing user-bound), and an overlay makes inheriting the
    default. Inheriting a credential is the wrong direction for the one
    assistant anybody standing in a room can talk to. So it is complete, and
    it has to stand on its own.

    `together_ai` was the example here and stopped being a fair one. A
    cross-provider fallback is routed to that provider's own client, and a
    config that does not declare it has no client to build -- so with the block
    absent the deployer wrote the fallback in and nanobot skipped it during a
    real OpenCode Zen outage, answering the room with an error. The credential
    was in that container's environment throughout; only the rescue was
    missing. What the rule protects is still protected: no scraper, and nothing
    belonging to one person.
    """
    from nanobot.config.schema import Config
    casa = REPO / "config" / "instances" / "house" / "config.json"
    assert casa.is_file()
    merged = json.loads(casa.read_text(encoding="utf-8"))
    Config.model_validate(merged)
    # The property that makes "complete, not an overlay" worth the duplication.
    assert "brightdata" not in merged.get("tools", {}).get("mcpServers", {})
    # Every provider it lists has to be one it can build: an entry with neither
    # a key nor a base is the shape that made the fallback inert.
    for name, block in (merged.get("providers") or {}).items():
        if not name.startswith("_"):
            assert "apiKey" in block or "apiBase" in block, name


def test_the_deployed_config_loads():
    """A comment at the top level fails validation; inside a section it does not.

    That asymmetry is invisible in a diff and fatal at startup.
    """
    from nanobot.config.schema import Config
    Config.model_validate(json.loads(DEPLOY_CONFIG.read_text()))


@pytest.fixture(scope="module")
def cfg():
    from nanobot.config.schema import Config
    return Config.model_validate(json.loads(DEPLOY_CONFIG.read_text()))


def test_n8n_is_pointed_at_the_server_and_not_at_the_container(cfg):
    """Omitted, this falls back to the schema default `http://localhost:5678`,
    which inside a container is the container: the skill cannot work."""
    assert cfg.n8n.base_url == "http://hub.home:5678"


def test_max_tokens_is_written_down_and_generous(cfg):
    """`maxTokens` has to be *set*, and set high.

    This test used to assert the opposite — that the key was absent, "left to
    the gateway". It is not left to anything: there is no omit path, so an
    absent key ships the schema default of 8192. And because these models
    reason out of the same budget they answer from, 8192 is not 8192 tokens of
    reply. The afiche brief that picked the Diseñador's model costs ~5k
    completion tokens uncapped; the same request capped at 6000 spent all of it
    reasoning and returned an empty message, which the family experiences as
    Alfred saying nothing at all.

    So the number is deliberate in both directions: present, so nobody reads
    the absence as "no ceiling", and far above what an ordinary turn uses, so
    the ceiling only ever catches a runaway.
    """
    raw = json.loads(DEPLOY_CONFIG.read_text())
    assert "maxTokens" in raw["agents"]["defaults"], (
        "absent does not mean unlimited — it means the schema default, 8192")
    assert cfg.agents.defaults.max_tokens >= 32768

    from nanobot.config.schema import AgentDefaults
    assert cfg.agents.defaults.max_tokens > AgentDefaults().max_tokens, (
        "if the schema default ever catches up, this file is no longer saying "
        "anything and the reasoning above needs re-checking")


def test_every_role_names_a_provider(cfg):
    """`modelProfiles` without the matching `providerProfiles` entry resolves
    through whichever provider claims the model name — silently, and not
    necessarily the intended one."""
    d = cfg.agents.defaults
    assert set(d.model_profiles) == set(d.provider_profiles), \
        "a profession with a model but no provider (or the reverse)"


def test_the_outage_fallback_is_set_and_is_not_the_model_it_rescues(cfg):
    """A fallback equal to the main model is not a fallback, it is a second
    attempt at the thing that just failed.

    On 2026-08-23 gpt-5.6-luna returned a hard 500 on every call at the gateway
    while other models answered on the same key. `providerRetryMode` here is
    `persistent`, which meant the house kept asking a broken model politely
    instead of asking a working one — so this key is what stands between one
    vendor-side outage and a household with no assistant at all.
    """
    d = cfg.agents.defaults
    assert d.model_fallback, "no outage fallback: a dead model takes the house down"
    # Every route gets the same fallback stamped on it, so it has to differ from
    # every route's model — not just the main one. Point modelFallback at
    # deepseek-v4-pro (the documented runner-up, and already modelPowerful and
    # subagentModel) and those routes silently lose their rescue while the main
    # one gains it.
    rescued = {d.model, d.subagent_model, d.subagent_model_powerful, d.model_powerful}
    rescued |= set(d.model_profiles.values())
    rescued.discard(None)
    assert d.model_fallback not in rescued, (
        f"{d.model_fallback} is itself one of the models it is supposed to rescue — "
        "for that route the fallback is a second attempt at what just failed")


def test_the_fallback_is_reachable_on_the_provider_that_would_use_it(cfg):
    """The swap happens inside one provider instance, on the same key and base
    URL — there is deliberately no providerFallback. So a fallback that belongs
    to some other provider is a key that can only ever produce a second error.

    `_outage_fallback_for` is what enforces this at build time; this pins that
    the deployed roster actually gets the fallback on every text route, so a
    provider move shows up here as a route that silently lost its rescue rather
    than at 3am during the next outage.
    """
    from nanobot.cli.commands import _outage_fallback_for

    d = cfg.agents.defaults
    routes = {
        "main": (d.provider, d.model),
        "subagent": (d.subagent_provider, d.subagent_model),
        "subagent powerful": (d.subagent_provider_powerful, d.subagent_model_powerful),
        "powerful": (d.provider_powerful, d.model_powerful),
    }
    for role, model in d.model_profiles.items():
        routes[f"profile {role}"] = (d.provider_profiles.get(role), model)

    for role, (provider_name, model) in routes.items():
        if not model and not provider_name:
            continue
        assert _outage_fallback_for(cfg, provider_name, model) == d.model_fallback, (
            f"route {role} runs on {provider_name}, which cannot route "
            f"{d.model_fallback} — that turn has no working fallback")


def test_the_vision_route_deliberately_has_no_fallback(cfg):
    """Different modality, different host. The fallback is a text model on the
    gateway; asking the house's Ollama for it produces one guaranteed 404 and
    an answer about an image nobody showed it."""
    from nanobot.cli.commands import _outage_fallback_for

    d = cfg.agents.defaults
    assert _outage_fallback_for(cfg, d.vision_provider, d.vision_model) is None


def test_the_key_the_schema_reads_is_the_key_the_file_writes(cfg):
    """`AgentDefaults` ignores unknown keys rather than rejecting them, so a
    `modelFallback` misspelled here would not fail the load — it would read as
    "no fallback configured" and only be discovered during the next outage."""
    raw = json.loads(DEPLOY_CONFIG.read_text())
    assert "modelFallback" in raw["agents"]["defaults"], \
        "the camelCase key the alias generator expects is absent"
    assert cfg.agents.defaults.model_fallback == \
        raw["agents"]["defaults"]["modelFallback"]


def test_the_house_instance_has_the_fallback_too():
    """The house instance is a whole separate deployment with its own config,
    and the outage hits it hardest: one container answers every voice turn in
    the house, so a dead model is somebody standing in a room talking to a
    speaker that stopped answering. It runs on `custom` like the per-member
    instances, so the same fallback routes.

    Its `config.json` is a whole file rather than an overlay, which is exactly
    how it came to be missed the first time: adding a key to the base does not
    reach it.
    """
    from nanobot.cli.commands import _outage_fallback_for
    from nanobot.config.schema import Config

    house = Config.model_validate(json.loads(
        (REPO / "config" / "instances" / "house" / "config.json")
        .read_text(encoding="utf-8")))

    d = house.agents.defaults
    assert d.model_fallback, "the voice instance has no outage fallback"
    assert d.model_fallback != d.model
    for role, (provider_name, model) in {
        "main": (d.provider, d.model),
        "subagent": (d.subagent_provider, d.subagent_model),
    }.items():
        assert _outage_fallback_for(house, provider_name, model) == d.model_fallback, \
            f"the voice {role} route cannot reach {d.model_fallback}"


def test_both_instances_keep_home_assistants_todo_list_out():
    """Home Assistant's to-do tools reach an empty list the family never uses;
    the shopping list and the chores are HomeCore's. Offered to the model, they
    were a real function beside a skill that is right, and function-calling
    models took the function: "no tasks" from the empty list, invented items
    written onto it.

    The house config is a whole file, not an overlay -- the case above is how
    a key added to the base once failed to reach it -- and the voice instance
    is the one where "add milk" is most often said out loud.
    """
    from nanobot.config.schema import Config

    wanted = {"todo_get_items", "HassListAddItem", "HassListCompleteItem",
              "HassListRemoveItem"}
    for path in (DEPLOY_CONFIG, REPO / "config" / "instances" / "house" / "config.json"):
        conf = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))
        ha = conf.tools.mcp_servers.get("homeassistant")
        assert ha is not None, f"{path.name}: no homeassistant server"
        missing = wanted - set(ha.disabled_tools)
        assert not missing, f"{path.relative_to(REPO)} offers {sorted(missing)} again"
