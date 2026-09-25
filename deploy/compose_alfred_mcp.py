#!/usr/bin/env python3
"""The per-member MCP bridges, written from the member list.

One container per person, and that is not a scaling decision -- it is what
makes the identity question answerable at all. The bridge holds one member's
credentials, so there is nothing for a tool call to choose: see
`services/alfred-mcp/alfred_mcp/identity.py`.

The single-container version of this was safe only because HomeCore refused to
route anybody else's Programmer to it. That refusal stays true; this is what
lets a second person be routed *somewhere* rather than nowhere.

Every property of an instance -- the container name, the port, the three ids
that name the person, and the two derived tokens -- comes from one member id,
which is what makes the class of bug `compose_members.py` documents impossible
here rather than merely absent. Read that file's header before changing this
one: the four live bugs it lists are all the same shape, a per-member value
that was right for four members out of five.
"""

from __future__ import annotations

import sys
from pathlib import Path

GENERATED = "docker-compose.members.yml"

# The values every bridge shares, as plain strings rather than inside the
# f-string below. Not style: `--check-contract` reads *this file's source* for
# `${VAR` literals -- a rendered unit has half its compose file in a Python
# module, and that is how the checks reach it -- and an f-string spells the
# same thing `${{VAR`, which matches nothing. A correct `state:` entry then
# looks decorative and a supplied variable looks unread.
SHARED_HOMECORE_URL = "${HOMECORE_CONTAINER_URL:-}"
SHARED_BROKER_URL = "${CODE_BROKER_URL:-http://code-broker:8910}"
SHARED_WORKSPACE = ("${CODE_WORKSPACE_DIR:"
                    "-/var/lib/home-stack/state/nanobot-code-workspace}")
# The same directory, named the way the *host* sees it -- the only name
# `opencode serve` can open, because it runs there and not in a container. The
# bridge is handed both and rewrites the broker's answers on the way out; see
# alfred_mcp/broker.py:retarget. The default matches the one above so that a
# deployment supplying neither names one real directory twice, rather than
# translating a correct path into a wrong one.
SHARED_WORKSPACE_HOST = ("${CODE_WORKSPACE_HOST_DIR:"
                         "-/var/lib/home-stack/state/nanobot-code-workspace}")

HEADER = """\
# GENERATED FILE -- every edit here is lost on the next deploy.
#
# Written by deploy/compose_alfred_mcp.py from `members[].programmer` in
# config/home-stack.yml -- the checkbox on each person's admin page. One
# bridge per member, each holding that person's credentials and nobody else's.
#
# Overlaid on docker-compose.yml, which holds only what they share -- the
# network they join to reach the code broker. Each block below says everything
# its container gets, because a shared template service is a service compose
# also starts.
#
# The port each one publishes is `port_base + <the member's position>`, the
# same arithmetic the assistants use. It is bound to 127.0.0.1 because what is
# behind it is one person's code and files, and the only thing that needs to
# reach it -- that member's `opencode serve` -- is on this host.
"""


def _deployer():
    """The deployer module, however it was loaded.

    Same trick as compose_members.py: this renderer must not reimplement
    `member_ids`, `share_folder`, the env-suffix rule or the token
    derivations, and the deployer is where all of those live.

    Found by what it *has* rather than by what it is called, because the
    deployer is loaded three ways: as `__main__` when somebody runs deploy.py,
    as `deploy` when a suite imports it, and as `deployer` from a file path
    when the admin page does. Naming only the first two made this renderer
    raise on the admin page's copy -- the one screen that writes
    `members[].programmer` in the first place.
    """
    wanted = ("member_ids", "share_folder", "member_env_suffix")
    for mod in list(sys.modules.values()):
        if mod is not None and all(hasattr(mod, n) for n in wanted):
            return mod
    raise RuntimeError("compose_alfred_mcp: the deployer module is not loaded")


def members(cfg: dict) -> list[str]:
    """Who gets a bridge: `members[].programmer`, on the admin page.

    A property of the person, not a list in a service block -- the same shape
    as `members[].whatsapp` and for the same reason. Household members are
    managed on the admin page and nowhere else, and "may Mora use the
    Programmer" is a fact about Mora.

    Off by default rather than everybody: each one is a container *and* an
    `opencode serve` process on a box that is already contending for a GPU, and
    the children have no broker workspace this stack ever created.

    An inactive member gets nothing, like the WhatsApp bridge above: the
    container would sit there holding credentials for somebody who has left.
    """
    return [str(m.get("id") or "").strip() for m in (cfg.get("members") or [])
            if str(m.get("id") or "").strip()
            and m.get("programmer") and m.get("active", True)]


def port_for(cfg: dict, mid: str) -> int:
    """This member's published port, from the base and their position.

    Position among the members who have it switched on, not in the household's
    whole list -- so switching it on for one person does not renumber
    somebody else's bridge and leave their opencode dialling a port that has
    moved.
    """
    conf = (cfg.get("services") or {}).get("alfred-mcp") or {}
    # `port:` is what `port_base:` used to be called, back when there was one
    # bridge. The deployer aliases it into the config before anything reads it,
    # but this renderer is also called with a bare config by the suites -- and
    # a household's moved port silently becoming 21071 is exactly the
    # per-member value that is right for nobody.
    base = int(conf.get("port_base", conf.get("port", 21071)))
    order = members(cfg)
    if mid not in order:
        raise KeyError(f"{mid} does not have the Programmer switched on")
    return base + order.index(mid)


def _member(cfg: dict, mid: str) -> dict:
    for m in (cfg.get("members") or []):
        if str(m.get("id") or "").strip() == mid:
            return m
    # Switched on for an id with no entry under `members:` at all.
    # Refused rather than defaulted: every id below is derived from this
    # record, and a bridge built from an empty one would authenticate nobody
    # while looking exactly like one that works.
    raise KeyError(
        f"{mid!r} has the Programmer switched on but is not a member of this "
        f"household")


def block(cfg: dict, mid: str) -> str:
    """One bridge, from one member id.

    Written out in full rather than `extends:`-ing a template service in the
    base file. A template service in the same project is a service compose
    *starts* -- one extra bridge holding nobody's credentials, answering on
    nobody's port -- and the alternative, hiding it behind a profile, is a
    second thing to keep true. The generated file says everything each
    container gets, exactly as compose_members.py does and for the same reason.
    """
    D = _deployer()
    suffix = D.member_env_suffix(mid)
    port = port_for(cfg, mid)
    folder = D.share_folder(_member(cfg, mid))
    return f"""
  alfred-mcp-{mid}:
    image: alfred-mcp:latest
    container_name: alfred-mcp-{mid}
    restart: unless-stopped
    # The portal is reached at the household's own name over a self-signed
    # certificate, which on a single-box install resolves through the host.
    extra_hosts:
      - "host.docker.internal:host-gateway"
    environment:
      # The three ids that name this person, each right in exactly one place:
      # the broker and the checkout are keyed on the member, every portal
      # request on the login, and paths on the share on the folder.
      - ALFRED_MCP_MEMBER={mid}
      - ALFRED_MCP_LOGIN=${{ALFRED_MCP_LOGIN_{suffix}:-}}
      - ALFRED_MCP_FOLDER={folder}
      # What this member's opencode must present. Derived per member, so a
      # token that leaked opens one person's bridge and nobody else's.
      - ALFRED_MCP_TOKEN=${{ALFRED_MCP_TOKEN_{suffix}:-}}
      - ALFRED_MCP_PORT={port}
      - CODE_BROKER_URL={SHARED_BROKER_URL}
      - CODE_BROKER_TOKEN=${{CODE_BROKER_TOKEN_{suffix}:-}}
      - HOMECORE_URL={SHARED_HOMECORE_URL}
      - HOMECORE_PROXY_TOKEN=${{HOMECORE_PROXY_TOKEN_{suffix}:-}}
      # Both names for the checkout root: this container's, and the host's.
      # What reads the answers this bridge returns is opencode, which opens the
      # paths in them from the host -- where the mount below does not exist.
      - CODE_WORKSPACE_DIR=/nanobot-code-workspace
      - CODE_WORKSPACE_HOST_DIR={SHARED_WORKSPACE_HOST}
    ports:
      # 127.0.0.1: what is behind this port is one person's code and files, and
      # the only thing that needs it -- that member's opencode -- is on this
      # host.
      - "127.0.0.1:{port}:{port}"
    volumes:
      # Read-only, and only so a file that already exists in a checkout can be
      # handed to the person. Writing to a checkout is the agent's own `edit`.
      - {SHARED_WORKSPACE}:/nanobot-code-workspace:ro
    healthcheck:
      # /healthz answers 503 when the broker is unreachable, which is the
      # failure that turns every git verb into a refusal while the container
      # itself looks perfectly fine.
      test: ["CMD", "python", "-c",
             "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:{port}/healthz', timeout=5).status == 200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
"""


def render(cfg: dict) -> str:
    ids = members(cfg)
    if not ids:
        # A valid compose file that starts nothing, rather than no file: the
        # unit names this overlay unconditionally, and a missing one is an
        # error from compose about a path rather than "nobody asked for this".
        return HEADER + "\nservices: {}\n"
    return HEADER + "\nservices:\n" + "".join(block(cfg, m) for m in ids)


def write(cfg: dict, unit_dir: Path) -> Path:
    """Write the overlay beside the compose file it extends, and return it."""
    path = Path(unit_dir) / GENERATED
    body = render(cfg)
    # Only when it changed: the deployer rsyncs this directory, and a file
    # whose mtime moves every run is one more thing that looks like a change.
    if not path.exists() or path.read_text(encoding="utf-8") != body:
        path.write_text(body, encoding="utf-8")
    return path


if __name__ == "__main__":
    import yaml
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import deploy  # noqa: F401  -- registers the module this renderer reads
    print(render(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))), end="")
