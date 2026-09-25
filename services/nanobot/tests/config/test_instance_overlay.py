"""The per-instance config overlay, and the config it produces.

`entrypoint.sh` copies config/config.json into every one of the per-member
containers, which was fine while every instance was identical. WhatsApp broke
that: it is linked to ONE person's account, so exactly one container may run
the channel. Enabling it in the base config would have the others retrying a
bridge that does not exist every five seconds, forever (channels/whatsapp.py's
start loop), so the difference has to live somewhere.

It lives in `config.<NANOBOT_INSTANCE>.json`, deep-merged over the copy, where
`null` deletes a key — an instance also has to be able to hold *fewer*
credentials than the base. These tests are about the thing that makes that
dangerous: the merged file is what the container validates at startup, and
`Config` forbids unknown keys at the top level — a mistake here is not a broken
feature, it is every member without an assistant until somebody reads a
container log.

The merge is reimplemented here rather than shelled out to, because the point is
to check the RESULT against the real schema; the shell is checked by `sh -n` and
by the deploy.
"""
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SHARED = REPO / "config"
DEPLOY_CONFIG = SHARED / "config.json"
# The house instance carries a *complete* config rather than an overlay: it
# holds fewer credentials than the base, and inheriting by default is the
# wrong direction for the one assistant anybody in a room can talk to.
HOUSE_CONFIG = SHARED / "instances" / "house" / "config.json"


def merge(base, over):
    """Same rule as the entrypoint: dicts merge, everything else replaces,
    `null` deletes."""
    for k, v in over.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def overlays():
    return sorted(SHARED.glob("config.user*.json"))


# The overlay a household writes is its own — it names a real WhatsApp account —
# so the package ships the template instead. Every invariant below is about the
# *shape* of an overlay, so they run against whichever exists: the real ones on
# a deployed house, the example here. Without this the whole file was four
# failures on a fresh clone, which teaches people to ignore it.
EXAMPLE = SHARED / "config.userN.json.example"


def overlays_or_example():
    return overlays() or ([EXAMPLE] if EXAMPLE.exists() else [])


def test_at_most_one_overlay_links_a_whatsapp_account():
    """Not a style rule — a scope check. A second overlay means a second
    WhatsApp account is linked, which is a decision with a ban risk attached and
    should not arrive as a surprise in a diff."""
    assert len(overlays()) <= 1, [p.name for p in overlays()]


@pytest.mark.parametrize("path", overlays(), ids=lambda p: p.name)
def test_an_overlay_is_valid_json(path):
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", overlays(), ids=lambda p: p.name)
def test_the_merged_config_still_loads(path):
    """The one that matters. An overlay that fails validation takes the whole
    instance down at startup, and it would be this file that caused it."""
    from nanobot.config.schema import Config

    merged = merge(json.loads(DEPLOY_CONFIG.read_text(encoding="utf-8")),
                   json.loads(path.read_text(encoding="utf-8")))
    Config.model_validate(merged)


@pytest.mark.parametrize("path", overlays(), ids=lambda p: p.name)
def test_an_overlay_only_says_what_differs(path):
    """An overlay that repeats the shared config is a second copy of it, and the
    two drift — which is the exact failure the single-config work was done to
    end. Only `_comment` keys may appear without changing anything."""
    from nanobot.config.schema import Config

    base = json.loads(DEPLOY_CONFIG.read_text(encoding="utf-8"))
    over = json.loads(path.read_text(encoding="utf-8"))
    merged = merge(json.loads(json.dumps(base)), over)
    assert Config.model_validate(merged) != Config.model_validate(base), (
        f"{path.name} changes nothing — delete it or fix it")


@pytest.mark.parametrize("path", [SHARED / "config.json", HOUSE_CONFIG,
                                  *overlays()], ids=lambda p: p.name)
def test_a_comment_never_references_an_env_var(path):
    """`config/loader.py` resolves `${VAR}` in **every** string it walks —
    `_resolve_env_vars` recurses through dicts and lists without caring what a
    key is called — and raises when the variable is unset. A `_comment` is a
    string like any other.

    So a comment that *mentions* an env var by reference demands it at startup.
    This was nearly shipped as a comment explaining that removing the WhatsApp
    credential is safe, written using the very syntax that would have made the
    container refuse to start without it. Spell the name out in prose instead.
    """
    import re

    data = json.loads(path.read_text(encoding="utf-8"))
    ref = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]")
        elif isinstance(node, str) and "_comment" in where and ref.search(node):
            raise AssertionError(
                f"{path.name} {where} references {ref.search(node).group()} in a "
                "comment — the loader will demand that variable at startup")

    walk(data, "")


def test_whatsapp_is_off_everywhere_except_the_overlay():
    """The shared config must never carry the channel: it reaches all five."""
    base = json.loads(DEPLOY_CONFIG.read_text(encoding="utf-8"))
    assert "whatsapp" not in base.get("channels", {}), (
        "config/config.json is mounted into every instance — a whatsapp "
        "block here links four people who did not ask for it")
    casa = json.loads(HOUSE_CONFIG.read_text(encoding="utf-8"))
    assert "whatsapp" not in casa.get("channels", {}), (
        "the voice assistant listens to anyone standing in a room")


def test_an_enabled_channel_never_has_an_empty_allow_from():
    """Empty `allowFrom` is **fatal at startup**, not a safe closed default.

    `channels/base.py` denies every sender on an empty list, which reads like
    fail-closed and is why this file shipped with one. But the channel manager
    validates it before any of that runs and nanobot *exits*: on 2026-08-16 the
    first deploy carrying this overlay put Alex's instance into a restart loop —
    65 of them — and he had no Alfred at all until it was found. The WhatsApp
    feature being off is a small thing; the assistant being gone is not.

    So the two fields move together: a channel is either disabled, or it names
    somebody.
    """
    for path in overlays():
        channels = json.loads(path.read_text(encoding="utf-8")).get("channels", {})
        for name, cfg in channels.items():
            if not isinstance(cfg, dict) or not cfg.get("enabled"):
                continue
            assert cfg.get("allowFrom"), (
                f"{path.name}: channel '{name}' is enabled with an empty allowFrom — "
                "nanobot will refuse to start and the instance will restart-loop")


def test_a_whatsapp_turn_can_never_reach_a_write_or_a_private_read():
    """`allowFrom` is `["*"]`, so this is what stands between a stranger's
    message and the household.

    The sender list used to be treated as the protection, and this test used to
    assert it was never `*`. That was the wrong place to hold the line: it
    guards whether a message becomes a turn, it never guarded reading, and
    maintaining every contact and group member by hand fails closed in the one
    way that takes the instance down. What actually protects the house is the
    read-only allowlist a third-party turn is held to — so that is what gets
    the test.

    Two properties, and the second is the one worth the file: no action that
    writes, and none that reads something private. Somebody messaging Alex's
    number must not be able to ask Alfred what is in Alex's chats.
    """
    from nanobot.agent.runner import (
        _ASK_READABLE_ACTIONS,
        _WHATSAPP_READABLE_ACTIONS,
    )

    # A WhatsApp turn gets its own, much shorter list. `ev-ask` is one of the
    # five people who live here; WhatsApp is anybody with the number. Sharing
    # one list put the saved places — the home address, the colegio — and the
    # family directory one "alfred, ..." away from a stranger in a group chat.
    assert _WHATSAPP_READABLE_ACTIONS < _ASK_READABLE_ACTIONS, (
        "the WhatsApp list must be a strict subset — a wider one is a leak, and "
        "an equal one means somebody merged them back together")

    personal = {
        # Who lives here, and where.
        "list_family", "get_profile", "search_family", "list_places",
        # What they owe, own and have to do.
        "list_chores", "list_recurring_chores", "my_points", "list_prizes",
        "list_redemptions", "list_groceries", "list_menu",
        # What they said, and to whom.
        "search_messages", "list_messages", "list_chats", "approve_reply",
        "search_notifications", "list_notifications", "reply_notification",
        "get_document", "download_document", "ask_family",
    }
    leaked = personal & _WHATSAPP_READABLE_ACTIONS
    assert not leaked, (
        f"a stranger on WhatsApp could ask Alfred about the household: {sorted(leaked)}")

    for name, actions in (("ask", _ASK_READABLE_ACTIONS),
                          ("whatsapp", _WHATSAPP_READABLE_ACTIONS)):
        writes = [a for a in actions
                  if not a.startswith(("list_", "get_", "search_", "my_"))]
        assert not writes, f"a {name} turn could reach a write action: {sorted(writes)}"

    # The ev-ask list may hold household things — the family asking each other
    # about chores is the whole point of it — but never somebody's private
    # correspondence.
    correspondence = {"search_messages", "list_messages", "list_chats",
                      "search_notifications", "list_notifications",
                      "reply_notification", "approve_reply"}
    assert not (correspondence & _ASK_READABLE_ACTIONS)


def test_whatsapp_only_answers_when_it_is_addressed_and_never_sends_by_default():
    """The two gates that replaced the sender list.

    `addressTrigger` decides what becomes a turn — without it, upstream's
    behaviour is to answer every allowed message, which with `allowFrom: ["*"]`
    would mean replying to every message the account receives. And nothing is
    ever sent until a chat is switched on: `reply_mode` starts at `off` in
    HomeCore, so a stranger gets silence whatever they write.
    """
    for path in overlays_or_example():
        wa = json.loads(path.read_text(encoding="utf-8"))["channels"]["whatsapp"]
        assert wa["addressTrigger"].strip(), (
            f"{path.name}: an empty trigger with allowFrom ['*'] answers "
            "everyone who messages the account")


def test_whatsapp_only_speaks_when_spoken_to():
    """An empty trigger restores upstream's answer-everything behaviour, which
    on a real person's account means replying to the family all day."""
    for path in overlays_or_example():
        over = json.loads(path.read_text(encoding="utf-8"))
        assert over["channels"]["whatsapp"]["addressTrigger"].strip(), path.name


def test_the_bridge_has_its_own_network_and_is_dialled_by_name():
    """The bridge is its own container on the compose network, not a tenant of
    nanobot-user2's namespace.

    This test used to assert the opposite — `network_mode: "service:..."` and a
    `127.0.0.1` bridgeUrl — because sharing the namespace was how the bridge
    stayed on loopback. A joined namespace dies with its owner, and user2
    restarts on every config change: the bridge spent 134 reconnects failing to
    resolve `web.whatsapp.com` against a DNS server that had been torn down,
    while the same name resolved fine from the container that owned it.

    So the three have to agree: no shared namespace, a service name in the URL,
    and a bridge that binds what BRIDGE_HOST says. The bind still defaults to
    loopback — the widening is the deployment's choice, made where it can be
    seen, and no ports are published so it reaches the compose network only.
    """
    for path in overlays_or_example():
        url = json.loads(path.read_text(encoding="utf-8"))["channels"]["whatsapp"]["bridgeUrl"]
        # The example is written for whichever member a household picks, so the
        # container name is asserted by shape rather than by member id.
        assert "whatsapp-bridge-user" in url, url
        assert "127.0.0.1" not in url, "a shared namespace is what this stopped relying on"

    # The bridge's compose block is NOT asserted here any more, and its absence
    # is the point rather than a gap. It used to be hand-written in
    # docker-compose.multiuser.yml; it is rendered by the deployer now, one
    # block per member with a linked phone. This test kept reading the old file
    # and died on `KeyError: whatsapp-bridge-user2` -- failing, and proving
    # nothing about the arrangement it is named after, for as long as that has
    # been true.
    #
    # It does not follow the block into the renderer because that is the other
    # side of the boundary: a service's own suite must not import the packaging
    # that deploys it. The three assertions moved intact to deploy/test_deploy.py
    # ("the whatsapp bridge is its own container"), which already renders the
    # member compose and is the layer that owns it.

    index = (REPO / "bridge" / "src" / "index.ts").read_text(encoding="utf-8")
    assert "process.env.BRIDGE_HOST || '127.0.0.1'" in index, (
        "the bind must still default to loopback for anyone running it outside compose")
