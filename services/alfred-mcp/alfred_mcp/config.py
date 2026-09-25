"""What this server was given, read once at start.

Everything here is per-*member*. One container serves one person, the way the
assistants do, and that is what keeps the identity question answerable: there is
no argument any tool can take that would make it act as somebody else, because
the process only holds one person's credentials.

Read at import and validated loudly. A missing credential here is a tool that
fails on the first call with a 401 from somewhere three services away, which
reads as "the Programmer is broken" rather than "nobody set a token".
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Something required is missing, said in the words needed to fix it."""


def _need(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is empty. The deployer supplies it from deploy/manifest.yml; "
            f"if you are running this by hand, see services/alfred-mcp/README.md.")
    return value


def _opt(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    # Who this container is. Three ids, and they are not interchangeable -- see
    # the table in CLAUDE.md. `member` is what the code broker and the
    # checkout directories are keyed on; `login` is what HomeCore's session
    # carries and what every /files/api request must present. Converting
    # between them happens here, once, and nowhere else in this service.
    member: str
    login: str
    # The third id, and the one paths on the share are made of:
    # `share_folder(member)`, the slugified display name. Not derivable from
    # either of the two above -- HomeCore resolves login -> folder with
    # `_files_access`, and this service has to name the folder in the `path` it
    # uploads to. Three ids name a person here and they are not
    # interchangeable; see the table in CLAUDE.md.
    folder: str

    # What opencode must present in the MCP `headers` block. Not a secret this
    # process derives -- it is handed the exact expected value, so the service
    # never holds a master secret it could mint another member's token from.
    token: str

    bind: str
    port: int

    broker_url: str
    broker_token: str

    homecore_url: str
    homecore_token: str

    # The checkout root, twice, because two processes with different filesystems
    # both have to name the same directory.
    #
    # The broker answers with its own path -- it runs in a container where the
    # workspace is mounted at `/nanobot-code-workspace`. The agent that receives
    # that answer is `opencode serve`, which runs on the **host**, where the same
    # directory is somewhere else entirely and `/nanobot-code-workspace` does not
    # exist at all. Handing the broker's path straight through told the agent to
    # read, grep and edit a directory it could not see: every tool call failed or
    # asked to leave its session directory, and the turn stopped without ever
    # saying why.
    #
    # So the bridge translates, once, on the way out -- which is what a bridge is
    # for, and the same "convert once, at the edge" rule the three person-ids
    # follow. Both empty means no translation, which is correct for a deployment
    # where the two are already the same path.
    workspace_broker: str = ""
    workspace_agent: str = ""

    # HomeCore serves a self-signed certificate on the home network. Every
    # other in-house caller passes verify=False for this -- the agent's usage
    # report, the homeweb relay, the WhatsApp channel, and the code broker,
    # which did *not* and failed every git verb with "certificate verify
    # failed" while its own health endpoint went on saying ok.
    verify_tls: bool = False


def load() -> Settings:
    return Settings(
        member=_need("ALFRED_MCP_MEMBER"),
        login=_need("ALFRED_MCP_LOGIN"),
        folder=_need("ALFRED_MCP_FOLDER"),
        token=_need("ALFRED_MCP_TOKEN"),
        bind=_opt("ALFRED_MCP_BIND", "0.0.0.0"),
        port=int(_opt("ALFRED_MCP_PORT", "21071")),
        broker_url=_need("CODE_BROKER_URL").rstrip("/"),
        broker_token=_need("CODE_BROKER_TOKEN"),
        homecore_url=_need("HOMECORE_URL").rstrip("/"),
        homecore_token=_need("HOMECORE_PROXY_TOKEN"),
        verify_tls=_opt("ALFRED_MCP_VERIFY_TLS", "") == "1",
        workspace_broker=_opt("CODE_WORKSPACE_DIR", "/nanobot-code-workspace"),
        workspace_agent=_opt("CODE_WORKSPACE_HOST_DIR", ""),
    )
