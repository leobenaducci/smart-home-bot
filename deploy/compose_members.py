#!/usr/bin/env python3
"""The per-member assistant containers, written from the member list.

`docker-compose.multiuser.yml` used to name five of these by hand --
`nanobot-user1` through `nanobot-user5`, ~100 near-identical lines apiece. Two
things were wrong with that, and only one of them is the obvious one.

**A sixth member got nothing, silently.** The deployer gates each instance on a
compose profile it builds from `services.nanobot.members`, so adding somebody
put `user6` into COMPOSE_PROFILES, into the port arithmetic, into the profile
generation and into the portal's roster -- and compose, asked to start a
profile that matches no service, starts no container and says nothing. Every
stage of that deploy reports success.

**And the five had already drifted.** Rendering them from one member each is
not a tidy-up; it is the fix for four bugs that were live on the household this
was extracted from, none of which any check could see:

  TASKS_API_URL      set on four of the five. `user5`'s assistant could not
                     reach the tasks API at all.
  FILE_SHARE_ADMIN   on user2 and user3. The admins are user1 and user2 -- so
                     one admin did not have it and one non-admin did.
  FILE_SHARE_FOLDER  the literal `userN`, while the portal serves that person's
                     files from `share_folder()` -- their first name, on a
                     share with years of files already in it. Every assistant
                     was looking in a directory that does not exist.
  WHATSAPP_ENABLED   user2's, hardcoded, along with a `whatsapp-bridge-user2`
                     service. `members[].whatsapp` decides it now, per person.

The identity of an instance -- `NANOBOT_INSTANCE`, `HOMECORE_USER_ID`,
`FILE_SHARE_FOLDER`, the volume paths, the profile, the ports, the name of
every per-member secret -- is derived here from a single member id, which is
the one property that makes the class of bug above impossible rather than
merely absent. `docs/kubernetes-plan.md` states the same requirement for the
rendered StatefulSets, and `deploy/kubernetes.py` has always met it; this is
the compose side finally doing the same.

Comments in the output are for whoever is reading it on the target box at 2am.
The reasoning lives here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The deployer, however it happens to be loaded, and it is loaded three ways:
# as `__main__` when somebody runs deploy.py, as `deploy` when a suite imports
# it, and as `deployer` from a file path when the admin page does. Finding the
# one already in memory rather than importing a fresh copy matters less for
# correctness -- everything used from here is a pure function -- than for not
# having two modules that each think they own the manifest.
_WANTED = ("member_ids", "share_folder", "member_env_suffix")
D = next((m for m in list(sys.modules.values())
          if m is not None and all(hasattr(m, n) for n in _WANTED)), None)
if D is None:
    import deploy as D  # noqa: E402

# The file the deployer writes beside the compose file it overlays. Not tracked
# -- its contents depend on who lives here -- and regenerated before every
# deploy and every contract check, so it can never be staler than the config.
GENERATED = "docker-compose.members.yml"

HEADER = """\
# GENERATED FILE -- every edit here is lost on the next deploy.
#
# Written by deploy/compose_members.py from `members:` and
# `services.nanobot.members` in config/home-stack.yml. One block per member,
# each derived from that member's id alone: change how an instance is built by
# editing the renderer, and add or remove a person by editing the config.
#
# Overlaid on docker-compose.multiuser.yml, which holds what the members share
# -- the init container, the code broker and the CLI.
#
# Nothing here uses a YAML anchor. Anchors do not cross files, and `<<:` merging
# an `environment:` list does not concatenate it -- it replaces it wholesale,
# which is a trap this stack has already paid for. A generated file can afford
# to say everything each container gets.
"""


def _ports(cfg: dict, index: int) -> list[str]:
    """The three published ports for the instance at `index`.

    From `services.nanobot.*_port_base`, which is where the portal already
    derives them from -- it is told the bases and adds the member's position.
    They were literals in the compose file, so moving a base renumbered what
    the portal dialled and left the containers where they were.
    """
    nano = (cfg.get("services") or {}).get("nanobot") or {}
    return [
        f"{int(nano.get('websocket_port_base', 21201)) + index}:8765",
        f"{int(nano.get('api_port_base', 21301)) + index}:8900",
        f"{int(nano.get('gateway_port_base', 21401)) + index}:18790",
    ]


def whatsapp_members(cfg: dict) -> list[str]:
    """The members whose assistant has a linked WhatsApp.

    Per member, because a household can have more than one and because the one
    that had it was written into the compose file by name. A member who is not
    active does not get a bridge: the container would sit there re-linking a
    phone for somebody who has left.
    """
    return [str(m.get("id") or "").strip() for m in (cfg.get("members") or [])
            if str(m.get("id") or "").strip()
            and m.get("whatsapp") and m.get("active", True)]


def _member(cfg: dict, mid: str) -> dict:
    for m in (cfg.get("members") or []):
        if str(m.get("id") or "").strip() == mid:
            return m
    # A member in `services.nanobot.members` with no entry under `members:`.
    # Rendered anyway, with the id standing in for everything derived from a
    # profile: an instance that starts and answers is a better failure than a
    # KeyError in the deployer, and the admin page is where the gap is fixed.
    return {"id": mid}


def member_env(cfg: dict, mid: str, index: int) -> list[str]:
    """Everything one instance's container is given, derived from `mid`.

    One function, one member id: `NANOBOT_INSTANCE`, `HOMECORE_USER_ID`, the
    file-share folder and the four per-member secret names cannot disagree with
    each other here the way five hand-written blocks could -- and did.
    """
    m = _member(cfg, mid)
    sfx = D.member_env_suffix(mid)
    env = [
        "OPENCODE_API_KEY=${OPENCODE_API_KEY}",
        # All four model endpoints, always set even when a provider is off:
        # nanobot reads its whole config for ${...} and refuses to start on an
        # *unset* reference, so a missing name is a container that never comes
        # up rather than a feature that is off.
        "OLLAMA_URL=${OLLAMA_URL:-}",
        "OLLAMA_API_KEY=${OLLAMA_API_KEY:-ollama}",
        "OLLAMA_CLOUD_URL=${OLLAMA_CLOUD_URL:-}",
        "OLLAMA_CLOUD_API_KEY=${OLLAMA_CLOUD_API_KEY:-disabled}",
        "OPENAI_COMPATIBLE_URL=${OPENAI_COMPATIBLE_URL:-}",
        "FREETOKEN_URL=${FREETOKEN_URL:-}",
        # The second ollama (vision). Defaulted like the rest: nanobot refuses
        # to start on an unset ${...}, so an endpoint nobody configured has to
        # be an empty string rather than absent.
        "OLLAMA_VISION_URL=${OLLAMA_VISION_URL:-}",
        "FREETOKEN_API_KEY=${FREETOKEN_API_KEY:-disabled}",
        "OPENAI_COMPATIBLE_API_KEY=${OPENAI_COMPATIBLE_API_KEY:-disabled}",
        "OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}",
        "OPENAI_API_KEY=${OPENAI_API_KEY:-}",
        "TOGETHER_API_KEY=${TOGETHER_API_KEY:-}",
        # The two image slots, chosen on the admin page's Models tab and
        # exported by `image_model_env()`. All four are passed even when the
        # slots are empty: the theme skill falls back to its own literal on an
        # empty string, and an *unset* name is what nanobot refuses to start
        # on. The two Together names carry the bare id that script sends
        # straight to the API; the neutral pair keep the provider prefix.
        "IMAGE_MODEL=${IMAGE_MODEL:-}",
        "IMAGE_MODEL_HIGH=${IMAGE_MODEL_HIGH:-}",
        "TOGETHER_IMAGE_MODEL=${TOGETHER_IMAGE_MODEL:-}",
        "TOGETHER_IMAGE_MODEL_HIGH=${TOGETHER_IMAGE_MODEL_HIGH:-}",
        "IMAGE_API_MODEL=${IMAGE_API_MODEL:-}",
        "IMAGE_API_MODEL_HIGH=${IMAGE_API_MODEL_HIGH:-}",
        "IMAGE_API_URL=${IMAGE_API_URL:-}",
        "IMAGE_API_URL_HIGH=${IMAGE_API_URL_HIGH:-}",
        "IMAGE_API_KEY=${IMAGE_API_KEY:-}",
        "HOMEASSISTANT_TOKEN=${HOMEASSISTANT_TOKEN:-}",
        # The base for the Home Assistant MCP entry. `/mcp_server/sse` is
        # appended in the nanobot config; the default here is the shape an
        # in-stack Home Assistant would have, never a household address.
        "HOMEASSISTANT_URL=${HOMEASSISTANT_URL:-http://127.0.0.1:8123}",
        # Empty on purpose: no LAN exemption unless the household names one.
        "NANOBOT_LAN_CIDR=${NANOBOT_LAN_CIDR:-}",
        # Where "the weather" means when nobody named a place. The weather skill
        # tries the person's last known fix and the saved "home" place first;
        # this is only the last resort. The manifest sets it from site.timezone,
        # whose last segment is a city. Empty is fine: the skill then says it
        # could not work one out instead of inventing somebody else's city.
        "SITE_LOCATION=${SITE_LOCATION:-}",
        "BRIGHTDATA_API_TOKEN=${BRIGHTDATA_API_TOKEN:-}",
        "CRAWL4AI_API_TOKEN=${CRAWL4AI_API_TOKEN:-}",
        "NANOBOT_N8N_API_KEY=${NANOBOT_N8N_API_KEY:-}",
        # Empty, not a name. `n8n.home` exists in this household's resolver
        # and points at the old stack on another box, which answers nothing --
        # so the obvious name is worse than none. The deployer exports the real
        # address; if that is missing, an empty base is a visible failure
        # rather than a silent request to a machine nobody meant.
        "NANOBOT_N8N_BASE_URL=${NANOBOT_N8N_BASE_URL:-}",
        f"PAPERLESS_API_TOKEN=${{PAPERLESS_API_TOKEN_{sfx}:-}}",
        "PAPERLESS_URL=${PAPERLESS_URL:-http://paperless.home:21030}",
        # The camera wall's API. Empty, not a name: the deployer exports the
        # real one from `dns.cameras` and `services.home-cameras.web_port`,
        # and camera-feed's own default was the port inside that container.
        "CAMERA_API_URL=${CAMERA_API_URL:-}",
        "NTFY_CREDENTIALS=${NTFY_CREDENTIALS:-}",
        "NTFY_BASE_URL=${NTFY_BASE_URL:-}",
        "SEARXNG_BASE_URL=${SEARXNG_BASE_URL:-}",
        "VANE_BASE_URL=${VANE_BASE_URL:-}",
        "SEARCH_LANGUAGE=${SEARCH_LANGUAGE:-en}",
        "HOMECORE_URL=${HOMECORE_URL:-}",
        "FILE_SHARE_HOST=${FILE_SHARE_HOST:-}",
        "FILE_SHARE_NAME=${FILE_SHARE_NAME:-share}",
        "FILE_SHARE_USERNAME=${FILE_SHARE_USERNAME:-share}",
        "FILE_SHARE_PASSWORD=${FILE_SHARE_PASSWORD:-}",
        # What the *house* calls this person, which is what the portal serves
        # their files under and what the share has years of files in. The five
        # hand-written blocks all said `userN`, which is a directory nobody has.
        f"FILE_SHARE_FOLDER={D.share_folder(m)}",
    ]
    # Sees everybody's folders on the share. From `members[].admin`, the one
    # place the household says who is a parent -- it was written into two of
    # the five blocks by hand, and neither of them matched.
    if m.get("admin"):
        env.append("FILE_SHARE_ADMIN=1")
    env += [
        "TASKS_API_URL=${TASKS_API_URL:-https://portal.home:21001/tasks/api}",
        # The **login**, not the member id. This becomes `X-Proxy-User` on every
        # call to the household's own APIs, and the portal starts by looking the
        # name up with `find_user()` -- so `user1` fails that lookup, `_proxy_auth`
        # returns without setting a session, and every request comes back
        # "No autenticado" with nothing in the log that names the cause.
        #
        # That is what the family saw: Alfred could not read a chore list, and
        # went looking for a way around it. Measured against the live portal on
        # 2026-08-30 -- member id: 401. Login id with the matching token: 200,
        # with the real balance in it.
        #
        # Everything else on this line stays the member id, and that is the rule
        # rather than an inconsistency: the instance name, the state directory
        # and the secret suffixes are what the *deployer* builds. Only what a
        # request carries keys on the login.
        f"HOMECORE_USER_ID={D.portal_logins(cfg).get(mid, '')}",
        f"HOMECORE_PROXY_TOKEN=${{HOMECORE_PROXY_TOKEN_{sfx}:-}}",
        f"NANOBOT_API_SECRET=${{NANOBOT_API_SECRET_{sfx}:-}}",
        # Where the broker answers, and the token that says which member this
        # instance may act for. Derived per member from CODE_BROKER_SECRET,
        # which this container never sees -- so an instance can act for itself
        # and for nobody else.
        "CODE_BROKER_URL=${CODE_BROKER_URL:-http://code-broker:8910}",
        f"CODE_BROKER_TOKEN=${{CODE_BROKER_TOKEN_{sfx}:-}}",
        # This member's place in the live list, for the heartbeat's phase
        # offset. Unset falls back to the digits in NANOBOT_INSTANCE, which are
        # not dense once a household has seen departures.
        f"NANOBOT_STAGGER_INDEX=${{NANOBOT_STAGGER_INDEX_{sfx}:-}}",
        # Names the per-instance config overlay (config/config.<member>.json),
        # merged over the base config by entrypoint.sh. No file, no effect.
        f"NANOBOT_INSTANCE={mid}",
    ]
    # The `whatsapp` skill gates on this (requires.env), so a member without a
    # linked phone is not offered a channel that would fail.
    if mid in whatsapp_members(cfg):
        env.append("WHATSAPP_ENABLED=1")
    env += [
        # Hard cap on one background task. The 20-minute default silently
        # truncated anything real.
        "NANOBOT_SUBAGENT_MAX_RUNTIME_S=${NANOBOT_SUBAGENT_MAX_RUNTIME_S:-14400}",
        # Unset leaves /v1/debug/* disabled, which is the safe default: those
        # routes return the agent's commands and their output.
        "NANOBOT_DEBUG_SECRET=${NANOBOT_DEBUG_SECRET:-}",
    ]
    return env


def member_volumes(mid: str) -> list[str]:
    return [
        "${NANOBOT_STATE_DIR:-/var/lib/home-stack/state/nanobot}/"
        f"{mid}:/home/nanobot/.nanobot",
        # Only this member's checkouts. The access list in the registry is a
        # claim until the filesystem agrees with it, and the path below the
        # root is the same inside the container and out.
        "${CODE_WORKSPACE_DIR:-/var/lib/home-stack/state/nanobot-code-workspace}/"
        f"{mid}:/nanobot-code-workspace/{mid}",
        "${NANOBOT_SHARED_STATE_DIR:-/var/lib/home-stack/state/nanobot-shared}"
        ":/shared-state",
        # One directory mount, not five single-file ones: a single-file bind
        # resolves to an inode and `git checkout` replaces these files rather
        # than editing them, so new content arrived on a new inode and the
        # container went on serving the old one. Read-only, deliberately: the
        # agent takes untrusted input, and a prompt injection that rewrote the
        # persona would reach every instance at once.
        "./config:/nanobot-config:ro",
        # Stays a file mount: the target is root-owned and the container runs
        # as uid 1000, so the entrypoint cannot symlink into /usr/local/bin.
        "./config/ntfy-send:/usr/local/bin/ntfy-send:ro",
    ]


def _block(name: str, lines: list[str]) -> str:
    return f"  {name}:\n" + "".join(f"    {ln}\n" if ln else "\n" for ln in lines)


def _listing(key: str, items: list[str], indent: str = "    ") -> list[str]:
    """`key:` and its items, with a `#` item passed through as a comment."""
    out = [f"{key}:"]
    for item in items:
        out.append(f"  {item}" if item.startswith("#") else f"  - {item}")
    return out


def agent_block(cfg: dict, mid: str, index: int) -> str:
    """One member's assistant."""
    lines = [
        f"container_name: nanobot-{mid}",
        "# Started only when `services.nanobot.members` lists this member.",
        f'profiles: ["{mid}"]',
        # One image for everyone, built by the deployer -- NOT a per-service
        # `build:` block, which makes compose build the same Dockerfile once
        # per member and export N byte-identical images.
        "image: alfred-nanobot:latest",
        "restart: unless-stopped",
        'user: "${UID:-1000}:${GID:-1000}"',
    ]
    lines += _listing("cap_drop", ["ALL"])
    lines += _listing("cap_add", ["SYS_ADMIN"])
    lines += _listing("security_opt", ["apparmor=unconfined", "seccomp=unconfined"])
    lines += _listing("extra_hosts", ['"host.docker.internal:host-gateway"'])
    # What a plugin contributes, written by the deployer into the staged build
    # context. Optional and usually absent. A file rather than more
    # `environment:` lines because the core compose cannot know a household
    # service's variable names.
    lines += ["env_file:", "  - path: plugin-contributions.env",
              "    required: false"]
    lines += _listing("environment", member_env(cfg, mid, index))
    lines += _listing("volumes", member_volumes(mid))
    lines += _listing("ports", _ports(cfg, index))
    lines += ["deploy:", "  resources:", "    limits:",
              '      cpus: "1"', "      memory: 1G"]
    lines += ['entrypoint: ["entrypoint.sh"]',
              "depends_on:", "  init-dirs:",
              "    condition: service_completed_successfully"]
    return _block(f"nanobot-{mid}", lines)


def bridge_block(mid: str) -> str:
    """One member's WhatsApp bridge, when they have a linked phone.

    It gets its own network. It used to share the agent's namespace
    (`network_mode: "service:nanobot-userN"`) so the bridge could stay on
    loopback, and that bought a failure worth more than it saved: **a joined
    namespace dies with its owner.** The agent restarted, Docker tore the
    namespace down and built a new one, and this container was left holding the
    corpse -- 134 reconnects failing with `EAI_AGAIN web.whatsapp.com` against
    an embedded DNS server that no longer existed. The agent restarts whenever
    config.json changes, which is most deploys, so it was not an edge.

    BRIDGE_HOST replaces it. No `ports:`, so 0.0.0.0 means the compose network,
    not the LAN. The widening is real: the other members can now reach the
    socket. They cannot use it -- the token lives on this member's volume and
    nowhere else.

    `entrypoint:` is load-bearing, and its absence is what kept this service
    from ever starting. The image declares ENTRYPOINT ["entrypoint.sh"], whose
    last line is `exec nanobot "$@"`, so `command: ["node", "dist/index.js"]`
    alone became `nanobot node dist/index.js` -- exit 2 on "No such command
    'node'" and a restart loop that reads exactly like a missing token.

    Overriding the entrypoint also keeps entrypoint.sh's `cp /etc/nanobot/
    config.json "$HOME/.nanobot/config.json"` away from this container.
    $HOME/.nanobot here is the member's *own* volume, and the baked file has no
    `whatsapp` block -- so every restart was overwriting their overlay-merged
    config with one that disables the very channel this service exists to serve.
    """
    lines = [
        f"container_name: whatsapp-bridge-{mid}",
        "# Its own profile, not the member's: a linked phone can be turned off",
        "# without taking the assistant with it.",
        f'profiles: ["whatsapp-{mid}"]',
        "image: alfred-nanobot:latest",
        "restart: unless-stopped",
        'user: "${UID:-1000}:${GID:-1000}"',
    ]
    lines += _listing("cap_drop", ["ALL"])
    lines += ["working_dir: /app/bridge",
              'entrypoint: ["node"]',
              'command: ["dist/index.js"]']
    lines += _listing("environment", [
        "BRIDGE_PORT=3002",
        "BRIDGE_HOST=0.0.0.0",
        # On this member's own volume, so the linked-device session survives a
        # redeploy. Losing it is not a restart, it is an errand: somebody has
        # to unlock a phone and scan a QR code again.
        "AUTH_DIR=/home/nanobot/.nanobot/whatsapp-auth",
    ])
    # No code workspace here on purpose. The bridge shares the member's data
    # volume but is not an agent: it has no reason to see the code, and a mount
    # nothing uses is one somebody later assumes is load-bearing.
    lines += _listing("volumes", [
        "${NANOBOT_STATE_DIR:-/var/lib/home-stack/state/nanobot}/"
        f"{mid}:/home/nanobot/.nanobot"])
    lines += ["deploy:", "  resources:", "    limits:",
              '      cpus: "0.5"', "      memory: 512M"]
    return _block(f"whatsapp-bridge-{mid}", lines)


def render(cfg: dict) -> str:
    members = D.member_ids(cfg)
    linked = whatsapp_members(cfg)
    out = [HEADER, "services:\n"]
    for index, mid in enumerate(members):
        out.append(agent_block(cfg, mid, index))
        if mid in linked:
            out.append(bridge_block(mid))
    if not members:
        # A `services:` key with nothing under it is not a valid compose file,
        # and a household part-way through its first install has no members
        # yet. The overlay adds nothing; the base file still stands alone.
        return HEADER + "services: {}\n"
    return "\n".join(out)


def write(cfg: dict, unit_dir: Path) -> Path:
    """Write the overlay beside the compose file it extends, and return it."""
    path = Path(unit_dir) / GENERATED
    body = render(cfg)
    # Only when it changed: the deployer rsyncs this directory, and a file
    # whose mtime moves every run is one more thing that looks like a change.
    if not path.exists() or path.read_text(encoding="utf-8") != body:
        path.write_text(body, encoding="utf-8")
    return path


def profiles(cfg: dict) -> list[str]:
    """The compose profiles that start what this household actually has."""
    return D.member_ids(cfg) + [f"whatsapp-{m}" for m in whatsapp_members(cfg)]


if __name__ == "__main__":
    import yaml
    print(render(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))), end="")
