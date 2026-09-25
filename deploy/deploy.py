#!/usr/bin/env python3
"""Deploy the home stack.

This is what replaced Jenkins. It reads config/home-stack.yml for where things
go and deploy/manifest.yml for how they get there, ships the service directory
to the target host over ssh, brings the compose project up, and then proves the
service answers before it calls the deploy done.

    ./home-stack list                 what exists, where it runs, if it's on
    ./home-stack deploy                  everything enabled, in dependency order
    ./deploy/deploy.py home-core nanobot     just these
    ./home-stack plan        print the plan, touch nothing
    ./deploy/deploy.py home-core --only proxy   one unit of one service

A push is not a deploy: nothing here is triggered by a commit. That was true of
the Jenkins jobs too and is worth keeping — say a change is merged and waiting,
never shipped, until this has run.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import ipaddress
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ollama_instances  # noqa: E402  (cloud.ollama.instances)

def _reexec_into_venv() -> None:
    """Re-run under the installer's virtualenv if PyYAML is missing here.

    install.sh creates .venv and installs PyYAML into it, but every script in
    this package is documented and shipped as `./deploy/deploy.py` with a
    `#!/usr/bin/env python3` line. On a host without system PyYAML that made the
    first command in the installer's own summary fail, with a hint to pip
    install -- which a PEP 668 distro then refuses as externally managed. Both
    remedies were dead ends, so find the venv instead.
    """
    venv_python = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(str(venv_python), [str(venv_python), *sys.argv])


try:
    import yaml
except ImportError:
    _reexec_into_venv()
    sys.exit(
        "PyYAML is missing and no .venv was found.\n"
        "Run ./home-stack install, which creates one."
    )

ROOT = Path(__file__).resolve().parent.parent
# Overridable, because the admin container mounts the *live* config and
# secrets at /state and exports these -- the copies baked into its image at
# build time are stale the moment the page saves anything. Hardcoding the paths
# meant Deploy from the admin page applied the build-time config and reported
# success, while the change a person just saved never reached a container.
MANIFEST = ROOT / "deploy" / "manifest.yml"


def _config_path() -> Path:
    """The site config that is actually authoritative.

    The same two-copy problem the credentials file has, and the same answer.
    `config/home-stack.yml` in the checkout is the seed: the admin unit copies
    it to `{paths.config}` on its first deploy and never again, because the
    page owns it afterwards and re-seeding would throw away what somebody set.

    So the deployed copy is the current one, and reading the seed instead means
    a shell deploy and a page deploy disagree about the household's own
    settings. That is not hypothetical: `paths.media` was changed on the admin
    page to /mnt/data/smart-bot while the checkout still said /mnt/data, and
    the next `./home-stack deploy home-cameras` from a shell would have pointed
    the camera wall at an empty directory and orphaned 115 GB of recordings --
    green, with nothing to notice.

    Prefer the deployed copy when it exists. HOME_STACK_CONFIG still wins over
    both, which is what the tests and the admin container set.
    """
    override = os.environ.get("HOME_STACK_CONFIG")
    if override:
        return Path(override)
    seed = ROOT / "config" / "home-stack.yml"
    try:
        cfg = yaml.safe_load(seed.read_text()) or {}
        live = Path((cfg.get("paths") or {}).get(
            "config", "/var/lib/home-stack/config")) / "home-stack.yml"
    except Exception:                                       # noqa: BLE001
        return seed
    return live if live.is_file() else seed


CONFIG = _config_path()


def write_in_place(tmp: Path, target: Path) -> None:
    """Move `tmp`'s bytes into `target` without changing target's inode.

    For `CONFIG` and the secrets file, and only because of what they are: both
    are **bind-mounted as single files** into the containers that read them, so
    the inode at that path is the identity every container resolved once at
    start and will never resolve again.

    A rename replaces the inode. On the host it succeeds and leaves every
    running container reading the file that used to be there -- the change is
    on disk, the page shows the old value, and nothing reports the difference.
    Inside a container the same rename raises EBUSY instead, which is at least
    loud. Neither is what a config write should do, so neither is what happens:
    the destination is opened and written, and it stays the same file.

    The cost is that a reader arriving mid-write can see a partial file, where
    a rename would have shown it strictly the old or the new one. That is
    accepted deliberately. `tmp` is fully serialised and validated before this
    is called, so the window is a single write of a known-good buffer and a
    failure in it raises. The alternative failure is silent and lasts until
    somebody recreates a container.

    `admin/app.py` carries the twin of this, for the same two files. They are
    apart because the admin page loads this module lazily and may not have it
    when it needs to save.
    """
    data = tmp.read_bytes()
    try:
        target.write_bytes(data)
    finally:
        tmp.unlink(missing_ok=True)


def config_divergence() -> str:
    """What the checkout says that the deployed copy does not, or "".

    Said out loud rather than resolved silently. Preferring the deployed copy
    is right -- it is the one the page edits and the one containers read -- but
    it makes a hand edit to the checkout do nothing, which is the same trap
    pointing the other way. Somebody who just edited a file deserves to be told
    it is not the one in use.
    """
    seed = ROOT / "config" / "home-stack.yml"
    if CONFIG == seed or not seed.is_file() or not CONFIG.is_file():
        return ""
    try:
        a = yaml.safe_load(seed.read_text()) or {}
        b = yaml.safe_load(CONFIG.read_text()) or {}
    except Exception:                                       # noqa: BLE001
        return ""

    def flat(d, prefix=""):
        out = {}
        for k, v in (d or {}).items():
            if isinstance(v, dict):
                out.update(flat(v, f"{prefix}{k}."))
            else:
                out[f"{prefix}{k}"] = v
        return out

    fa, fb = flat(a), flat(b)
    differing = sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
    if not differing:
        return ""
    shown = ", ".join(differing[:6]) + ("..." if len(differing) > 6 else "")
    # Names the live path first and says what to do about it. The old wording
    # led with the checkout's filename, which is the wrong half to put in front
    # of somebody: on 2026-09-01 two separate readers -- one auditing the
    # backups from another repository -- reached a wrong conclusion from the
    # checkout copy and were one step from acting on it. Both had checked
    # carefully; both had checked the file with the obvious name in the obvious
    # place, and neither ran this tool, which is the only thing that was saying
    # otherwise.
    #
    # So it also names the one source of truth that cannot be read wrong: what
    # the container is actually running with. A config file says what somebody
    # intended; only the container says what took effect, and the gap between
    # them is where that afternoon went.
    # `paths.config` is the exception and is called out on its own line. Every
    # other key in the seed is ignored, so a long divergence list is normal and
    # harmless -- but that key is what `_config_path()` reads out of the seed to
    # decide which file is authoritative in the first place. A disagreement
    # there does not mean "the seed is stale", it means the two copies point at
    # different config directories, and whichever one the deployer reaches
    # first wins. Buried among 58 other names, nobody would see it.
    key = "paths.config"
    lede = (f"{CONFIG} is the config in use. config/home-stack.yml in the "
            f"checkout is the seed: the deployer reads `paths.config` out of "
            f"it to find this file, and ignores the rest -- so editing "
            f"anything else there does nothing.\n")
    if key in differing:
        lede += (f"    But {key} DISAGREES between them, and that is the one "
                 f"key the seed is still read for: {fa.get(key)!r} in the "
                 f"checkout against {fb.get(key)!r} deployed.\n")
    return (lede
            + f"    {len(differing)} setting(s) differ: {shown}\n"
            f"    What a service is actually running with is not in either "
            f"file: ask the container.\n"
            f"      docker inspect <container> "
            f"--format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}'")


def _secrets_path() -> Path:
    """The credentials file that is actually authoritative.

    There are two. `secrets/smart-home-bot.env` in the checkout is the seed:
    the deployer copies it to `{paths.config}` on a service's *first* deploy
    and never again, because the admin page owns it after that and re-seeding
    would throw away what somebody set there.

    Which means the deployed copy is the current one, and reading the seed
    instead is how a key set on the admin page never reaches a deploy. That
    happened: a household pasted a working model key into the Credentials
    screen, the page wrote the deployed copy, and `./home-stack deploy` shipped
    the stale one out of the checkout -- so the assistant went on failing with
    the same auth error the key was meant to fix, and both files looked set.

    Prefer the deployed copy when it exists. `HOME_STACK_SECRETS` still wins
    over both, which is what the tests and the admin container set.
    """
    override = os.environ.get("HOME_STACK_SECRETS")
    if override:
        return Path(override)
    seed = ROOT / "secrets" / "smart-home-bot.env"
    try:
        cfg = yaml.safe_load((ROOT / "config" / "home-stack.yml").read_text()) or {}
        live = Path((cfg.get("paths") or {}).get(
            "config", "/var/lib/home-stack/config")) / "smart-home-bot.env"
    except Exception:                                       # noqa: BLE001
        return seed
    return live if live.is_file() else seed


SECRETS = _secrets_path()


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

class Out:
    """Terminal output. Quiet on success, loud and specific on failure."""

    def __init__(self, color: bool = True):
        self.color = color and sys.stdout.isatty()

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def step(self, msg: str) -> None:
        print(self._c("36", f"==> {msg}"))

    def sub(self, msg: str) -> None:
        print(f"    {msg}")

    def ok(self, msg: str) -> None:
        print(self._c("32", f"    ok  {msg}"))

    def warn(self, msg: str) -> None:
        print(self._c("33", f"    warn {msg}"))

    def fail(self, msg: str) -> None:
        # Flushed so the two streams interleave in the order things happened.
        # Piped into a file or the admin page, an unflushed stdout put the
        # summary above the run it summarised.
        sys.stdout.flush()
        print(self._c("31", f"    FAIL {msg}"), file=sys.stderr)
        sys.stderr.flush()


out = Out()


class DeployError(Exception):
    """Anything that should stop this service's deploy and be reported."""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise DeployError(
            f"{shown_path(path)} does not exist. "
            f"Copy {path.stem}.example{path.suffix} to it, or run install.sh."
        )
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def shown_path(path: Path) -> str:
    """A path as a person would recognise it, wherever it actually is.

    `relative_to` raises when the target is outside the repository, and both
    the config and the secrets file legitimately live elsewhere -- the admin
    container reads them from {paths.config}, and a test points them at a
    scratch directory. A message about a file was aborting on the file's
    location.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_secrets(path: Path) -> dict[str, str]:
    """Parse the env file. Absent keys and empty keys are the same thing here:
    a key present but empty used to reach a container as an empty string, which
    fails as a model outage or a silent 401 rather than as a config error."""
    if not path.exists():
        raise DeployError(
            "secrets/smart-home-bot.env does not exist. "
            "Copy secrets/smart-home-bot.env.example to it and fill it in, "
            "or run: ./home-stack install --generate-secrets"
        )
    if path.stat().st_mode & 0o077:
        out.warn(f"{shown_path(path)} is readable by others; chmod 600 it")

    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeployError(f"secrets/smart-home-bot.env:{lineno}: not KEY=value")
        key, _, val = line.partition("=")
        val = val.strip().strip("'\"")
        if val:
            values[key.strip()] = val
    return apply_secret_aliases(values)


# The portal was called `home-web`, and its credentials were named after it.
# Renaming the service renames the variables; an env file written before that
# still has the old ones, and it is the file a household edited by hand and
# the installer generated. Read the old name when the new one is absent, so an
# upgrade is a pull rather than an editing job.
#
# Old wins nothing: if both are present the new name is used, which is what
# makes this safe to leave in place indefinitely.
SECRET_ALIASES = {
    "HOMECORE_SECRET_KEY": "HOMEWEB_SECRET_KEY",
    "HOMECORE_DEBUG_API_KEY": "HOMEWEB_DEBUG_API_KEY",
    # 2026-09-01: the stack moved off the flat OpenCode Go plan onto Zen, which
    # is the same account and the same key against a different endpoint. The
    # credential did not change, only what it is called -- so an env file that
    # still says OPENCODE_GO_API_KEY keeps working and nobody has to re-paste a
    # key to take the upgrade. The endpoint move is not optional though: see
    # the warning by `assistant.models` in the shipped config.
    #
    # And the new name carries no plan in it, deliberately. One OpenCode key
    # opens both endpoints -- measured, both answer 200 on the same bearer --
    # so naming it after Go or after Zen names the endpoint this stack happens
    # to point at today rather than the thing in the file, which is an account
    # credential. `OPENCODE_ZEN_API_KEY` was the name for part of one
    # afternoon and never reached an env file anywhere, which is why it is not
    # aliased here: an alias for a name nothing ever wrote is a line that can
    # only ever mislead the next person reading this list.
    "OPENCODE_API_KEY": "OPENCODE_GO_API_KEY",
}


def apply_secret_aliases(values: dict[str, str]) -> dict[str, str]:
    """Fill in a renamed secret from the name it used to have."""
    for new, old in SECRET_ALIASES.items():
        if new not in values and old in values:
            values[new] = values[old]
    # The per-member proxy tokens are numbered, so they cannot be listed.
    for key, val in list(values.items()):
        if key.startswith("HOMEWEB_PROXY_TOKEN_"):
            values.setdefault("HOMECORE_PROXY_TOKEN_" + key[len("HOMEWEB_PROXY_TOKEN_"):], val)
    return values


def interpolate(value, cfg: dict):
    """Resolve {services.home-core.port} and friends against the config.

    Dotted names are looked up literally first, so a key containing a dot --
    `services.nanobot-house.gateway_port` -- works without quoting rules.
    """
    if isinstance(value, dict):
        return {k: interpolate(v, cfg) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, cfg) for v in value]
    if not isinstance(value, str):
        return value

    def resolve(match: re.Match) -> str:
        path = match.group(1)
        node = cfg
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise DeployError(
                    f"config has no '{path}' (needed by the manifest)"
                )
            node = node[part]
        return str(node)

    return re.sub(r"\{([a-zA-Z0-9_.-]+)\}", resolve, value)


# How a model in `assistant.models` names the provider it wants. Three, and
# all three can be live at once: the assistant can answer on OpenCode Zen while
# the cameras are read by a model on your own GPU.
# How a model in `assistant.models` names where it runs. A bare name is
# OpenCode Zen; anything else carries its provider as a prefix, so one role can
# run on your own GPU while another goes to a paid API and a third somewhere
# else again -- which is the point of having four of them.
# What never travels into a pushed tree or a staged build context. `.env` and
# `secrets` are the ones that matter: an asset staged into a build context ends
# up in an image layer, and an image is a thing that gets copied around.
TREE_EXCLUDES = (
    ".git", "__pycache__", "*.pyc", ".venv", "node_modules",
    ".env", "secrets", ".pytest_cache", ".mypy_cache",
)


MODEL_PROVIDERS = {
    "ollama": "ollama",              # your own Ollama, on your own hardware
    "ollama-cloud": "ollama_cloud",  # ollama.com
    "openrouter": "openrouter",      # openrouter.ai, one key for many models
    "together": "together_ai",       # together.ai
    "openai": "openai",              # OpenAI itself
    # Anything that speaks the OpenAI API at a URL you give it: a vLLM or
    # llama.cpp server on the LAN, LM Studio, or a provider this package has
    # never heard of. One entry rather than one per vendor, because the only
    # thing that varies is the address -- see cloud.openai_compatible.
    "openai-compatible": "openai_compatible",
    # A local MoE serving engine, and a slot of its own rather than a second
    # use of `openai-compatible:`. There is exactly one of those, and spending
    # it on FreeToken would mean a household running FreeToken can no longer
    # point at a vLLM box, an LM Studio, or the provider this package has never
    # heard of -- which is the whole job that entry exists for.
    "freetoken": "freetoken",
    # A second ollama, for vision/OCR. Its own prefix rather than a flag on
    # `ollama:`, because it is a different server: OLLAMA_NUM_PARALLEL and
    # OLLAMA_CONTEXT_LENGTH are server-global, so the only way to give the text
    # roles four small fast slots and an image turn one slot with its own
    # window is to run two and name them apart.
    "ollama-vision": "ollama_vision",
}


# Which role a sub-agent follows when the household names none of its own.
#
# Module-level rather than a local in apply_model_choices, because the admin
# page shows what a blank field will actually run and must not answer that
# question from a second copy of this map. It already loads this module; a
# blank picker that displayed the wrong inherited model would be worse than
# one that displayed nothing.
# `subagent_powerful` was retired on 2026-09-24: a background task runs on the
# sub-agent model, and hard work somebody waits on is the powerful model's, in
# the chat. A config that still names it is ignored (apply_model_choices).
# The planner and the plan steps (work of several steps in the chat, nanobot's
# tools/plan.py) follow the models they stand in for, the same way.
SUBAGENT_INHERITS = {"subagent": "everyday", "planner": "everyday", "plan_steps": "subagent"}


# Models to fall back to after the household's own choice, in order.
#
# The point is *family*, not quality. On 2026-08-30 the everyday model answered
# 500 on every attempt and the configured fallback answered `Model is disabled`
# in the same minute -- one provider-side change took the rescue out along with
# the thing it was rescuing, and every Alfred in the house said "Internal server
# error" to whoever was typing. A second candidate is only worth having if it
# fails for different reasons than the first.
#
# So: one from each family the gateway routes, cheapest-useful first. Kept here
# rather than in the assistant's config because it is a property of *this
# gateway* -- which models exist and who makes them -- and the deployer is the
# thing that already knows a role from a provider. `./home-stack models` prints
# the live state of every one of these.
#
# Anything unreachable is simply skipped at runtime, so a name going dark costs
# one wasted attempt rather than a broken deploy. Being wrong here is cheap;
# being empty is what was expensive.
# Re-picked for Zen on 2026-09-01. These are bare names, so they have to be
# names *this* gateway routes, and moving off the flat Go plan changed which
# ones those are: `glm-5.3-flash` and `qwen3.8-flash` are on Go's roster and
# not on Zen's, so two thirds of this ladder became a pair of 404s on the end
# of every exhausted retry -- silently, because a fallback is only ever walked
# during an outage nobody is watching.
# None of these three is a shipped role default, and that is the constraint
# that picked them. A name a role already uses is dropped from the chain -- a
# role's own model is not a rescue for that role -- so a ladder built from the
# obvious candidates quietly shrinks: `gpt-5.6-luna` is the shipped `everyday`
# and five other roles, and `deepseek-v4-flash` is the shipped `fallback`, so
# a stock install would get one extra name here, not three. Three families,
# none of them the house's own, is what this was for.
#
# Two of the three went on 2026-09-12, both measured on the house's key:
#   gemini-3.5-flash-lite  Zen serves Gemini only on Google's own shape,
#                          /v1/models/<name>:generateContent (200 there). On
#                          /chat/completions and /responses -- the only two
#                          nanobot speaks -- it is a 500, for every household,
#                          and a 500 reads as transient and gets retried.
#   qwen3.5-plus           401 "Model is disabled" on this account. An account
#                          matter, so it may belong back here for another one.
# What remains is one family after the household's own chain, which is less
# than this was meant to be and more than two dead names at the end of it.
OUTAGE_FALLBACKS = (
    "minimax-m3",             # 512k of context, and a cache price
)


def split_model(value: str) -> tuple[str, str]:
    """`ollama-cloud:qwen3-vl:8b` -> ("qwen3-vl:8b", "ollama_cloud").

    Split once, on the first colon only: an Ollama model name contains one of
    its own (`qwen3-vl:8b`), and splitting greedily turned the tag into the
    provider and left the model called `qwen3-vl`.
    """
    text = str(value or "").strip()
    prefix, sep, rest = text.partition(":")
    if sep and prefix in MODEL_PROVIDERS and rest:
        return rest, MODEL_PROVIDERS[prefix]
    # `ollama-<id>:` -- any instance in cloud.ollama.instances. By shape, so a
    # model naming a removed instance still splits into its own provider and
    # fails as "not configured" rather than going to Zen as an unknown name.
    local = ollama_instances.provider_of_prefix(prefix) if sep and rest else ""
    if local:
        return rest, local
    return text, "custom"


# Where a hosted provider is reached, and the credential that opens it. The
# same four bases nanobot's config.json carries; written down here as well
# because two consumers outside nanobot -- the portal's chat titler and
# Paperless's AI -- take a plain OpenAI-compatible URL and key from their
# environment and have no provider table of their own.
HOSTED_API_BASE = {
    "custom": ("https://opencode.ai/zen/v1", "OPENCODE_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "together_ai": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}


def ollama_context(cfg: dict, provider: str) -> str:
    """The context window the household's own Ollama behind `provider` serves
    (`cloud.ollama.local.context`, `cloud.ollama.vision.context`), or "" when
    the config does not say.

    The stack does not run those servers, so it cannot know their
    OLLAMA_CONTEXT_LENGTH -- but a consumer that names a window of its own must
    name that one. Ollama reloads a model whose requested context differs from
    the one it holds, so Paperless asking for its default 8192 from a server
    at 65536 would reload the model on every document, and evict whatever the
    assistants had resident.
    """
    iid = ollama_instances.id_of_provider(provider)
    inst = ollama_instances.by_id(cfg).get(iid) if iid else None
    if not inst:
        return ""
    # A legacy entry that never named a window says nothing, rather than the
    # normalizer's default: an invented window reloads the model on every call.
    conf = (cfg.get("cloud") or {}).get("ollama") or {}
    if conf.get("instances") is None:
        legacy = conf.get({"main": "local", "vision": "vision"}.get(iid, ""), {}) or {}
        if not legacy.get("context"):
            return ""
    return str(inst["context"])


def model_endpoint(cfg: dict, secrets: dict, value: str,
                   host_network: bool = False) -> tuple[str, str, str]:
    """`(bare name, OpenAI-compatible base ending in /v1, key)` for a model
    written the way `assistant.models` writes one. `("", "", "")` when blank.

    For the consumers that talk to a provider directly rather than through
    nanobot: they get one URL and one key, so the prefix has to be resolved
    here, against the same tables nanobot's config is built from.

    `host_network` is for home-core, which runs on the host's network: the
    container-facing `host.docker.internal` resolves nowhere there, so the
    local Ollama is handed out at the host's own address (`ollama_host`). A
    vision URL typed with that name means "the host this container runs on",
    and for a host-networked consumer that host is itself -- so it becomes
    loopback, the same answer `_from_this_container()` in admin/app.py gives.
    Rewriting it to the compute host's address instead was right only on a
    one-box install, and pointed the titler at the wrong machine on a split
    one. A provider that is switched off yields an empty URL -- the caller
    falls back rather than exporting an address nothing answers at.
    """
    name, provider = split_model(value)
    if not name:
        return "", "", ""
    endpoints = ollama_endpoints(cfg, secrets)
    url, key = "", ""
    if provider == "ollama":
        url, key = endpoints.get("ollama_host" if host_network else "ollama", ("", ""))
    elif provider == "ollama_cloud" or ollama_instances.is_local_provider(provider):
        url, key = endpoints.get(provider, ("", ""))
        # Another local instance takes no credential. The literal "ollama" is
        # what every other local consumer sends and what its compose default
        # falls back to, so a chosen model and a blank one agree.
        if provider != "ollama_cloud":
            key = "ollama"
        if host_network and url:
            parsed = urlsplit(url)
            if parsed.hostname == "host.docker.internal":
                netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
                url = urlunsplit(parsed._replace(netloc=netloc))
    elif provider == "openai_compatible":
        url, key = openai_compatible_endpoint(cfg, secrets)
    elif provider == "freetoken":
        url, key = freetoken_endpoint(cfg, secrets)
    elif provider in HOSTED_API_BASE:
        base, secret = HOSTED_API_BASE[provider]
        key = secrets.get(secret, "")
        url = base if key else ""
    if not url:
        return name, "", ""
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    return name, url, key or "ollama"


def container_address(cfg: dict, role: str, setting: str) -> str:
    """The address a bridge-networked container should dial `role` at.

    `from_container` when there is one -- `add_container_addresses` writes
    `host.docker.internal` there for a loopback role -- and the plain address
    otherwise, which is right on a split install where the two agree.

    `.get("address")` rather than `["address"]`: admin/app.py does
    `hosts.setdefault(role, {})` for all three roles and only writes `address`
    when the form field was filled, so a role really can exist with none. A
    KeyError there is a traceback naming a dict key; this names the setting.
    """
    hosts = cfg.get("hosts") or {}
    if role not in hosts:
        raise DeployError(
            f"{setting} is {role!r}, which is not a role under hosts:")
    reach = str((hosts[role] or {}).get("from_container")
                or (hosts[role] or {}).get("address") or "").strip()
    if not reach:
        raise DeployError(
            f"{setting} is {role!r}, and hosts.{role} has no address: to dial")
    return reach


def ollama_instance_url(cfg: dict, inst: dict) -> str:
    """Where a container reaches one local Ollama instance (no trailing /v1)."""
    if inst["url"]:
        return inst["url"]
    if inst.get("legacy_url"):
        # The old vision entry carried its URL outright; kept verbatim so a
        # config nobody has touched deploys exactly as it did.
        return inst["legacy_url"]
    # The container-facing address. The consumers of this URL are containers
    # -- the two assistants, the camera wall's clip reviewer and Paperless's
    # own AI -- and on the default one-PC install the role's `address` is
    # 127.0.0.1, which inside a bridge-networked container is the container
    # itself.
    #
    # One URL for every consumer only works while no consumer is on host
    # networking, where this name resolves nowhere. home-core is exactly that
    # shape and is handed this variable, and is safe only because its compose
    # file does not list it. `loopback_reachability_problems` says so out loud
    # if that changes, and test_deploy.py asserts it over the shipped package
    # -- so it is caught rather than discovered.
    reach = container_address(cfg, inst["host"], f"cloud.ollama.instances[{inst['id']}].host")
    return f"http://{reach}:{inst['port']}"


def _vision_url(cfg: dict, endpoints: dict, local_url: str) -> str:
    vision = str(((cfg.get("assistant") or {}).get("models") or {}).get("vision") or "")
    provider = split_model(vision)[1] if vision else ""
    if ollama_instances.is_local_provider(provider) and provider in endpoints:
        return endpoints[provider][0]
    return endpoints.get("ollama_vision", (local_url, ""))[0]


def ollama_endpoints(cfg: dict, secrets: dict) -> dict[str, tuple[str, str]]:
    """{provider: (base url, api key)} for whichever Ollamas are switched on.

    Both may be. The local one is where a model runs on hardware you own and
    the images never leave the house; the cloud one is ollama.com. A model
    says which it wants, so a house can read its cameras locally and answer
    chat from somewhere else.

    The local key is the literal "ollama" and is never empty on purpose:
    nanobot resolves `${VAR}` in every string in its config and refuses to
    start on an unset reference — the failure is a container that never comes
    up, not a feature that is off.
    """
    conf = (cfg.get("cloud") or {}).get("ollama") or {}

    # `mode: local|cloud` is what this used to be. Translated rather than
    # rejected, so a config written before both could be on at once still
    # deploys.
    if "mode" in conf and "local" not in conf and "cloud" not in conf:
        legacy = str(conf.get("mode", "local")).lower()
        conf = {
            "local": {"enabled": legacy == "local", "host": conf.get("host", "compute"),
                      "port": conf.get("port", 11434)},
            "cloud": {"enabled": legacy == "cloud",
                      "url": conf.get("cloud_url", "https://ollama.com")},
        }

    out_map: dict[str, tuple[str, str]] = {}

    # Every local instance (cloud.ollama.instances, or the old local/vision
    # entries read as that list), under its provider name. The benchmark's is
    # left out: no role runs on it.
    hosts = cfg.get("hosts") or {}
    for inst in ollama_instances.serving(cfg):
        provider = ollama_instances.provider_of(inst["id"])
        out_map[provider] = (ollama_instance_url(cfg, inst), "ollama")
        if provider == "ollama" and not (inst["url"] or inst.get("legacy_url")):
            # The same endpoint as the host sees it. home-core is on host
            # networking, where `host.docker.internal` does not resolve at all.
            # Two names because there are two right answers, exactly as with
            # `address` and `from_container`.
            out_map["ollama_host"] = (
                f"http://{hosts[inst['host']]['address']}:{inst['port']}", "ollama")

    # ollama.com is on when its key is set, and off when it is not. There is no
    # `enabled:` to agree with: a switch beside a credential is two things to
    # get wrong, and the pair used to disagree in the expensive direction --
    # `enabled: true` with an empty key was a hard deploy failure over a
    # provider nobody was using. Same rule as openrouter, together and openai.
    cloud = conf.get("cloud") or {}
    key = secrets.get("OLLAMA_API_KEY", "")
    if key:
        out_map["ollama_cloud"] = (
            str(cloud.get("url") or "https://ollama.com").rstrip("/"), key)

    return out_map


def docker_socket_gid() -> str:
    """The group that owns /var/run/docker.sock, as a number.

    The admin container drops to the deploying user and would otherwise lose
    the socket with it. Read off the socket rather than assumed: the docker
    group's gid differs between distributions, and a wrong number is a
    container that starts fine and cannot deploy anything.
    """
    try:
        return str(os.stat("/var/run/docker.sock").st_gid)
    except OSError:
        # No socket here -- a remote-only deploy, or a machine that has not
        # installed docker yet. `0` is honest: it grants nothing extra.
        return "0"


def host_addresses(cfg: dict, role: str = "hub") -> list[str]:
    """Every address a browser might use to reach `role`.

    What this exists for is `browsable_address()` below, which picks the one a
    dashboard tile should link to. It has to contain what a person actually
    types, and what a person types is usually neither `localhost` nor
    127.0.0.1 -- it is the machine's address on the LAN, from a phone.

    `hosts.<role>.address` is 127.0.0.1 on a single-box install, so it cannot
    be the whole answer. When the role is this machine the real interfaces are
    enumerated; when it is somewhere else the configured address is what there
    is, and is right.
    """
    hosts = cfg.get("hosts") or {}
    configured = (hosts.get(role) or {}).get("address", "127.0.0.1")
    found = ["localhost", "127.0.0.1", configured]
    if configured in _LOOPBACK:
        # Only for a local role. Reading this machine's interfaces to answer a
        # question about another machine would be worse than not answering.
        try:
            # `ip addr`, not `hostname -I`: the latter returns every address on
            # the box including bridge gateways nothing reaches it by -- it
            # offered 192.168.0.1 here, which is not an interface anybody can
            # use. Filtering by interface name is what keeps docker's own
            # networks out.
            out_ = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "scope", "global"],
                capture_output=True, text=True, timeout=5)
            for line in out_.stdout.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                iface, cidr = parts[1], parts[3]
                if iface.startswith(("docker", "br-", "veth", "virbr")):
                    continue
                found.append(cidr.split("/")[0])
        except (OSError, subprocess.SubprocessError):
            pass
        if not any(a not in _LOOPBACK and a != "localhost" for a in found):
            # No `ip` binary. The admin image has none, and this function is
            # what decides the address in every dashboard tile, every ntfy
            # link and every `{derived.*_self_url}` -- so a deploy from that
            # page wrote 127.0.0.1 into all of them, which opens the *phone*.
            #
            # A datagram socket "connected" to a documentation address sends
            # nothing and never leaves the machine; it just asks the kernel
            # which local address it would route from. No binary, no
            # dependency, and it answers the question the `ip` parse above was
            # approximating.
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    probe.settimeout(1)
                    probe.connect(("192.0.2.1", 9))  # TEST-NET-1, RFC 5737
                    found.append(probe.getsockname()[0])
                finally:
                    probe.close()
            except OSError:
                pass
    seen, keep = set(), []
    for a in found:
        a = a.strip()
        if not a or a in seen:
            continue
        seen.add(a)
        keep.append(a)
    return keep


def browsable_address(cfg: dict, role: str = "hub") -> str:
    """The address a browser on somebody's phone can actually open.

    `hosts.<role>.address` is 127.0.0.1 on a single-box install, and a link
    to 127.0.0.1 opens the *phone*. Every tile on the household dashboard was
    built from it, so every one of them was a dead link from anything but the
    hub itself -- which is the one device nobody browses from.

    The `dns:` name is not used, deliberately. Those are the names people type
    and they only work where something answers them: `house.home` does not
    resolve on this network while `cameras.home` does, so half the tiles would
    work and half would not, for a reason nothing on the page could explain. An
    address on the LAN needs no DNS at all.
    """
    for candidate in host_addresses(cfg, role):
        if candidate not in _LOOPBACK and candidate != "localhost":
            return candidate
    return (cfg.get("hosts") or {}).get(role, {}).get("address", "127.0.0.1")


def member_notification_topics(cfg: dict) -> dict[str, str]:
    """`NTFY_TOPIC_<MEMBER>` for anyone whose topic is not their member id.

    The portal derives a member's ntfy topic from their folder name --
    `user1` becomes `User1` -- and lets `NTFY_TOPIC_<FOLDER>` override it.
    Nothing supplied that variable, so a household that already had ntfy
    published to topics named after the ids this package generates, while every
    phone in the house was subscribed to the names people use. The
    notifications went out and nobody got them.

    So a member may carry `ntfy_topic:`. Absent, it falls back to the display
    name, which is what an existing install almost always used -- the old
    portal derived the topic from a folder named after the person.
    """
    out_map = {}
    for member in cfg.get("members") or []:
        mid = str(member.get("id") or "").strip()
        if not mid:
            continue
        topic = str(member.get("ntfy_topic")
                    or member.get("display_name") or "").strip()
        if not topic or topic.capitalize() == mid.capitalize():
            continue
        out_map[f"NTFY_TOPIC_{mid.upper()}"] = topic
    return out_map


def share_folder(member: dict) -> str:
    """The folder on the share this person's files live in.

    What the house calls somebody, which is their first name and not the
    `user4` this package generates for them. An install carried across from an
    older setup already has those folders on the share with years of files in
    them, and the portal finds them by this name and no other.

    `folder:` wins, and exists so that renaming somebody on the admin page
    cannot move their files: display names are edited, folders are not.
    Without one, the display name is slugified -- accents folded, spaces
    dropped -- which is what those older setups used and what a fresh install
    would want anyway.
    """
    mid = str(member.get("id") or "").strip()
    explicit = str(member.get("folder") or "").strip()
    if explicit:
        return explicit
    name = unicodedata.normalize("NFKD", str(member.get("display_name") or ""))
    slug = "".join(c for c in name.lower()
                   if c.isascii() and (c.isalnum() or c in "-_"))
    return slug or mid


def portal_logins(cfg: dict) -> dict:
    """`{member id: login id}` from the portal's user store.

    People sign in with a login id that is not their member id -- the store is
    the only thing that knows which is which. Read rather than derived, and
    tolerant of not being there: on a machine that has not deployed the portal
    yet there is no store, and a service whose authorisation table comes out
    empty refuses everybody, which is the right way for one to fail.
    """
    # `.get`, because this is called from `derive()` -- which runs before a
    # config has necessarily been filled in, and a suite's fixture config has
    # no `paths:` at all. No store is the same answer as an unreadable one.
    state = (base_paths(cfg) or {}).get("state")
    if not state:
        return {}
    path = Path(state) / "home-core" / "users.json"
    try:
        users = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for user in users if isinstance(users, list) else []:
        member = str(user.get("member") or "").strip()
        login = str(user.get("username") or "").strip()
        if member and login:
            out[member] = login
    return out


def member_locales(cfg: dict) -> str:
    """`"<login id>:<locale>,..."` -- who reads the portal in what language.

    Keyed on the login, because this is read from a request: the session
    carries that id and nothing else. `members[].locale` is the setting the
    admin page already writes and the deployer already uses for the assistant's
    own profiles; the portal never received it at all, so every member got the
    house default and `_pick_locale`'s promise of "then the member's saved
    preference" had nothing to consult.

    A member with no locale is left out rather than defaulted here -- the portal
    already falls back to the house language, and saying `es` twice is not the
    same as saying nothing.
    """
    logins = portal_logins(cfg)
    pairs = []
    for member in (cfg.get("members") or []):
        mid = str(member.get("id") or "").strip()
        locale = str(member.get("locale") or "").strip()
        login = logins.get(mid)
        if login and locale:
            pairs.append(f"{login}:{locale}")
    return ",".join(pairs)


# Tesseract's name for each locale this stack ships -- only for the ones the
# paperless-ngx image carries, which bundles deu, eng, fra, ita and spa. zh and
# ja are left out rather than named: a language the image lacks fails every
# consumption, and installing one (PAPERLESS_OCR_LANGUAGES) is not wired here.
_OCR_LANGUAGES = {"en": "eng", "es": "spa", "fr": "fra", "it": "ita", "de": "deu"}


def ocr_language(cfg: dict) -> str:
    """Paperless's OCR languages, `+`-joined: the house language first, then
    each member's, then English.

    It was `eng`, in the compose file, for a house whose documents are mostly
    Spanish -- and the Spanish data was in the image the whole time. Tesseract
    reads the first language as the main one, so the order is the setting's
    order; English goes last because it is in most paperwork anyway.
    """
    locales = [(cfg.get("locale") or {}).get("default")]
    locales += [m.get("locale") for m in (cfg.get("members") or [])]
    out = []
    for loc in locales + ["en"]:
        code = _OCR_LANGUAGES.get(str(loc or "").strip().lower().replace("_", "-").split("-")[0])
        if code and code not in out:
            out.append(code)
    return "+".join(out)


def member_logins(cfg: dict) -> str:
    """`"<login id>:<folder>,..."` -- the join a service is given when a token
    proves a login and the thing it may touch is a folder.

    People sign in with an id that is not their member id, and only the portal's
    user store knows which is which. A member with no portal account is simply
    absent: there is no request they could make.
    """
    logins = portal_logins(cfg)
    by_id = {str(m.get("id") or "").strip(): m for m in (cfg.get("members") or [])}
    pairs = []
    for mid in member_ids(cfg):
        login, member = logins.get(mid), by_id.get(mid)
        if login and member is not None:
            pairs.append(f"{login}:{share_folder(member)}")
    return ",".join(pairs)


def admin_logins(cfg: dict) -> str:
    """The login ids of the members `members[].admin` marks as parents.

    Exported rather than left to a service's own default, because a default is
    a list of people any copy of that service would let in.
    """
    logins = portal_logins(cfg)
    by_id = {str(m.get("id") or "").strip(): m for m in (cfg.get("members") or [])}
    return ",".join(logins[mid] for mid in member_ids(cfg)
                    if logins.get(mid) and (by_id.get(mid) or {}).get("admin"))


def member_share_folders(cfg: dict) -> str:
    """`user1:ana,user2:bo` -- the map the portal keys its share access on."""
    return ",".join(
        f"{m['id']}:{share_folder(m)}" for m in (cfg.get("members") or [])
        if str(m.get("id") or "").strip())


def notification_urls(cfg: dict) -> tuple[str, str]:
    """(as the host dials it, as a container dials it) for notifications.

    `cloud.notifications.mode` decides which ntfy the house publishes to:

      local     the container this stack deploys on the hub
      external  one you host elsewhere, at `external_url`
      off       none, and every consumer gets an empty string

    This was hardcoded to the local container in all three places that export
    NTFY_BASE_URL, so `mode: external` was a setting the config accepted, the
    service description advertised, and nothing ever read -- the portal went on
    publishing to the local address while the household's real ntfy sat
    unreferenced. A setting that never reaches a container is worse than one
    nobody changed.

    Two values because they differ only in the local case, where the host says
    127.0.0.1 and a bridge-networked container has to say something else. An
    external URL is a name both can resolve, so it is returned twice.
    """
    conf = (cfg.get("cloud") or {}).get("notifications") or {}
    mode = str(conf.get("mode", "local")).lower()
    if mode == "off":
        return "", ""
    if mode == "external":
        url = str(conf.get("external_url") or "").strip().rstrip("/")
        # A person types a hostname, and a hostname is not a URL: without a
        # scheme this reaches the containers as `ntfy.example.com` and every
        # publish fails on it. https, not http -- an ntfy somewhere else is
        # reached over the internet, and quietly picking the plaintext one
        # would put the household's notifications on the wire in clear.
        if url and "://" not in url:
            url = f"https://{url}"
        if not url:
            raise DeployError(
                "cloud.notifications.mode is 'external' but external_url is "
                "empty.\n    Give it the address of your ntfy, or switch the "
                "mode back to 'local'.")
        return url, url
    hosts = cfg.get("hosts") or {}
    hub = hosts.get("hub") or {}
    port = ((cfg.get("services") or {}).get("ntfy") or {}).get("port", 21031)
    return (f"http://{hub.get('address', '127.0.0.1')}:{port}",
            f"http://{hub.get('from_container') or hub.get('address', '127.0.0.1')}:{port}")


def openai_compatible_endpoint(cfg: dict, secrets: dict) -> tuple[str, str]:
    """(base url, api key) for `cloud.openai_compatible`, or ("", "").

    One provider entry rather than one per vendor. Everything in this class --
    vLLM, llama.cpp's server, LM Studio, LocalAI, and most hosted APIs that are
    not OpenAI -- differs only in its address, so what a household configures
    is an address and a key.

    The URL is normalised to end without `/v1`. People paste both forms from a
    provider's own documentation, and nanobot appends the path itself: the
    doubled `/v1/v1/chat/completions` that produces is a 404 on every turn,
    which is exactly the NANOBOT_URL failure this package already has a test
    for. Stated here rather than left to whoever writes the config.
    """
    conf = (cfg.get("cloud") or {}).get("openai_compatible") or {}
    if not conf.get("enabled"):
        return "", ""
    url = str(conf.get("url") or "").strip().rstrip("/")
    if not url:
        raise DeployError(
            "cloud.openai_compatible.enabled is true but its url is empty.\n"
            "    Give it the server's base address, or switch it off.")
    if url.endswith("/v1"):
        url = url[:-3]
    key = secrets.get("OPENAI_COMPATIBLE_API_KEY", "")
    # No refusal on an empty key: plenty of servers in this class take none at
    # all -- a llama.cpp or vLLM instance on the LAN is the ordinary case --
    # and demanding one would make the common setup impossible to express.
    return url, key


# What makes each provider usable, in the words somebody would need to fix it.
# `custom` is the bare-name case -- OpenCode Zen -- whose key is required for
# the whole unit, so it is never the thing that is missing here.
PROVIDER_REQUIREMENT = {
    "ollama": "cloud.ollama.local.enabled",
    "ollama_cloud": "the OLLAMA_API_KEY credential",
    "openrouter": "the OPENROUTER_API_KEY credential",
    "together_ai": "the TOGETHER_API_KEY credential",
    "openai": "the OPENAI_API_KEY credential",
    "openai_compatible": "cloud.openai_compatible",
    "freetoken": "cloud.freetoken",
    "ollama_vision": "cloud.ollama.vision",
}


def freetoken_endpoint(cfg: dict, secrets: dict) -> tuple[str, str]:
    """(base url, api key) for `cloud.freetoken`, or ("", "").

    Same shape as `openai_compatible_endpoint` and deliberately not the same
    entry. FreeToken runs on the household's own hardware and is the sort of
    thing that stays configured for months; the generic slot is where you put
    whatever you are trying this week. Sharing one entry would make those two
    mutually exclusive for no reason other than that both speak the same API.

    The key is optional and genuinely so -- a serving engine on your own desk
    usually has none -- which is why the provider spec carries `is_local`:
    without it nanobot refuses to build a keyless provider.
    """
    conf = (cfg.get("cloud") or {}).get("freetoken") or {}
    if not conf.get("enabled"):
        return "", ""
    url = str(conf.get("url") or "").strip().rstrip("/")
    if not url:
        raise DeployError(
            "cloud.freetoken.enabled is true but its url is empty.\n"
            "    Give it the server's base address, or switch it off.")
    # Same normalisation, same reason: people paste both forms out of a
    # provider's own docs and nanobot appends the path itself, so a trailing
    # `/v1` becomes `/v1/v1/chat/completions` and 404s every turn.
    if url.endswith("/v1"):
        url = url[:-3]
    return url, secrets.get("FREETOKEN_API_KEY", "")


def alias_alfred_mcp_port(cfg: dict) -> None:
    """`services.alfred-mcp.port` was what `port_base` is now. Read it.

    The bridge used to be one container on one `port:`; it is now one per
    member counting up from `port_base:`. A config written before that carries
    only the old name, and two things go wrong without this: the manifest's
    `{services.alfred-mcp.port_base}` hard-fails `interpolate` -- "config has
    no ...", on a service that was deploying yesterday -- and a household that
    had moved the port off 21071 gets the shipped default back silently,
    because the renderer's own fallback is a constant rather than their
    setting.

    Written into the config rather than read at each use, because the manifest
    interpolates the key literally and cannot be given a fallback. Old names
    for config keys are aliased on read here, as everywhere else.
    """
    conf = (cfg.get("services") or {}).get("alfred-mcp")
    if isinstance(conf, dict) and "port_base" not in conf and "port" in conf:
        conf["port_base"] = conf["port"]


def opencode_port_base(cfg: dict) -> int:
    """The first member's `opencode serve` port; the rest count up from it.

    A base rather than a URL, because this stack now *runs* these -- one
    systemd user unit per member -- where it used to be pointed at a single one
    somebody else had started. The same arithmetic the assistants use, and for
    the same reason: a port derived from a member's position cannot be right
    for one member and wrong for the next.
    """
    conf = (cfg.get("cloud") or {}).get("opencode") or {}
    return int(conf.get("port_base", 4096))


def opencode_servers(cfg: dict) -> dict[str, str]:
    """`{member: url}` for every member with an opencode of their own, or {}.

    Empty when `cloud.opencode` is off, and empty is the whole switch: the
    Programmer falls back to the nanobot Alfred for everybody. Falling back is
    right rather than a half-working feature, because the two backends do the
    same job.

    Loopback addresses, and correct as such: the only consumer is HomeCore,
    which runs `network_mode: host` and therefore shares this namespace.
    `host.docker.internal` -- right for every bridge-networked consumer in the
    manifest -- resolves nowhere there. It is also what lets anything reach an
    opencode bound to loopback, which is the whole boundary in front of an
    agent that has a shell.
    """
    conf = (cfg.get("cloud") or {}).get("opencode") or {}
    if not conf.get("enabled"):
        return {}
    members = _generator(GENERATORS["alfred-mcp"]).members(cfg)
    if not members:
        # Said, not refused. A household upgrading into this has nobody ticked
        # yet, and failing the deploy would stop *everything* over one
        # profession that has a working fallback -- the assistant's Programmer,
        # which is what they had before this existed. So: switched on with
        # nobody to serve behaves exactly as switched off, and says so.
        out.warn("cloud.opencode is on but nobody has the Programmer switched "
                 "on, so no opencode is configured. Tick it on somebody's page "
                 "in the admin; until then their Programmer runs on the "
                 "assistant.")
        return {}
    base = opencode_port_base(cfg)
    return {m: f"http://127.0.0.1:{base + i}" for i, m in enumerate(members)}


# The credentials opencode is handed, when the household has them. Named rather
# than swept from the secrets file: this decides what the Programmer can spend,
# and a rule like "anything ending in _API_KEY" would quietly hand it the
# camera's, the document store's and the notification service's as well.
#
# OPENCODE_API_KEY is deliberately absent. opencode holds its own OpenCode
# credential in auth.json, put there by `opencode auth login`, and that is the
# one it uses for Zen and Go -- this stack has never read it and should not
# start now.
OPENCODE_PROVIDER_KEYS = (
    "TOGETHER_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OLLAMA_API_KEY",
)


def opencode_api(cfg: dict) -> str:
    """Which of opencode's two HTTP surfaces to drive: `v1` or `v2`.

    They are served by one process and are not a rename of each other. v2 puts
    the agent and the working directory on the session, streams per session
    rather than globally, and -- on 1.18.27 -- carries no text deltas, so an
    answer arrives whole instead of word by word.

    `v2` names the `/api/*` routes on the OpenCode **1** binary. It is not
    OpenCode 2, which is a separate program (`@opencode-ai/cli`, binary
    `opencode2`) this stack has never run.

    **v1 is not the timid choice here.** On 1.18.27 an `/api` session resolves
    opencode's own thirteen tools and none of the MCP ones, while `GET /mcp`
    still reports the bridge connected -- so the Programmer loses the broker,
    the share and delegation, and keeps `bash`, which is git without the
    broker. Its prompts are also sometimes admitted and then never run. The
    same agent and model on v1 calls `alfred_list_projects` on the first turn.
    `docs/opencode-programmer.md` has the measurements.

    Refused rather than defaulted when it is neither: a typo would quietly mean
    v1, and the household would be told nothing while wondering why the
    Programmer still streams.
    """
    given = ((cfg.get("cloud") or {}).get("opencode") or {}).get("api")
    # Absent means v1; anything *written* is read as written. `or "v1"` would
    # have swallowed the ones worth refusing -- YAML reads a bare `no` as the
    # boolean false, and a household that typed something has said something.
    raw = "v1" if given is None else str(given).strip().lower()
    if raw not in ("v1", "v2"):
        raise DeployError(
            f"cloud.opencode.api is {raw!r}; it must be v1 or v2.\n"
            f"    v1 streams word by word. v2 puts the agent on the session "
            f"and answers in one piece.")
    return raw


def _opencode_restart_remedy(target) -> str:
    """What to actually do about an opencode-serve that would not restart.

    Two different failures wore one message. A household that has never run
    `install --opencode` has no units, and installing them is the answer. But a
    deploy launched from the **admin page** runs inside the admin container,
    which has no systemd at all -- there is not even a `systemctl` binary in
    that image -- so it can never restart a host process, however many times
    somebody installs the units. Telling that household to install them sends
    them round a loop, which is worse than saying nothing.

    So ask which it is, and answer that one.
    """
    have = target.run("command -v systemctl >/dev/null 2>&1", check=False)
    if getattr(have, "returncode", 1) != 0:
        return ("This deploy cannot reach systemd -- which is what a deploy "
                "launched from the admin page does, the container having none. "
                "The files are written; run `./home-stack deploy alfred-mcp` "
                "from a shell on this machine to restart it.")
    return "Run ./home-stack install --opencode to install the units."


def opencode_model(cfg: dict) -> str:
    """Which model opencode should answer the Programmer with, or "".

    `"<providerID>/<modelID>"`, written into the agent's own front matter --
    opencode's native way of pinning a model to an agent -- rather than passed
    on every request. Nothing in this stack calls it: opencode owns the
    provider, the credential and the routing, and this only says which one.

    It is deliberately **not** a role in `assistant.models`. That list is the
    roster of models *nanobot* calls, and every entry in it is something this
    deployer can build a provider for. An `opencode:` entry there was a
    category error, and it showed: it needed a prefix in MODEL_PROVIDERS, an
    exception in apply_model_choices so the profession's fallback would not be
    handed a provider nanobot cannot build, and a check to refuse the prefix on
    every other role. One setting in the block that already configures opencode
    costs none of that.

    Empty means opencode decides. On an account with OpenCode Zen that is a
    *free* model -- `big-pickle` at the time of writing -- and the free tier's
    own documentation says collected data may be used to improve the model.
    What the Programmer reads is this house's code, so a household that cares
    about that names one here rather than leaving it.
    """
    conf = (cfg.get("cloud") or {}).get("opencode") or {}
    return str(conf.get("model") or "").strip()


def enabled_providers(cfg: dict, secrets: dict, endpoints: dict) -> set[str]:
    """Which providers a model may actually name.

    `endpoints` covers the two Ollamas, which need an address as well as a
    credential. The rest need only their key, so the key is the whole answer --
    and an empty one means the provider is off rather than broken.
    """
    on = {p for p in endpoints
          if p == "ollama_cloud" or ollama_instances.is_local_provider(p)}
    on.add("custom")                      # OpenCode Zen, required for the unit
    for provider, key in (("openrouter", "OPENROUTER_API_KEY"),
                          ("together_ai", "TOGETHER_API_KEY"),
                          ("openai", "OPENAI_API_KEY")):
        if secrets.get(key, ""):
            on.add(provider)
    if openai_compatible_endpoint(cfg, secrets)[0]:
        on.add("openai_compatible")
    if freetoken_endpoint(cfg, secrets)[0]:
        on.add("freetoken")
    return on


def check_models_reachable(cfg: dict, providers: set) -> None:
    """Refuse a model whose provider is switched off.

    Naming `ollama-cloud:` with no key does not degrade to anything -- the
    container starts and every turn fails -- so it is better said while
    somebody is looking at a deploy.

    This used to be told only about the Ollamas, so a model on any of the four
    other providers skipped the check entirely: `openrouter:` with no key
    deployed green and failed at the first call, which is the same silence the
    missing provider block produced.
    """
    for persona, value in ((cfg.get("assistant") or {}).get("models") or {}).items():
        _, provider = split_model(value)
        if provider in providers:
            continue
        need = PROVIDER_REQUIREMENT.get(provider) or (
            f"a cloud.ollama.instances entry with id "
            f"{ollama_instances.id_of_provider(provider)!r}"
            if ollama_instances.is_local_provider(provider) else provider)
        raise DeployError(
            f"assistant.models.{persona} is {value!r}, but {need} is not set.\n"
            f"    Set it, or choose a model from another provider."
        )


# The roles whose consumer calls the provider itself instead of going through
# nanobot: home-core's chat titler and Paperless's AI.
DIRECT_ROLES = ("titles", "documents")


def check_direct_models(cfg: dict) -> None:
    """Refuse a `titles` or `documents` model its consumer cannot call.

    Both consumers speak `/v1/chat/completions` and nothing else. OpenCode
    Zen serves its GPT-5-class models on `/v1/responses` only and answers
    500 on chat completions -- nanobot copes because its provider routes by
    model, but these two have no such routing. The turn would not fail
    loudly: the titler returns '' on any error, so the chat simply never gets
    a name, and Paperless logs one failure per scan.

    The rule is `wants_responses()` in deploy/models.py, loaded rather than
    copied: it is one of the three copies that already exist, and it lives
    beside this file.
    """
    models = (cfg.get("assistant") or {}).get("models") or {}
    wants_responses = _generator("models").wants_responses
    for role in DIRECT_ROLES:
        value = str(models.get(role) or "").strip()
        name, provider = split_model(value)
        if value and provider == "custom" and wants_responses(name):
            raise DeployError(
                f"assistant.models.{role} is {value!r}, which OpenCode Zen "
                f"serves on /v1/responses only.\n    "
                f"{'The chat titler' if role == 'titles' else 'Paperless'} "
                f"calls /v1/chat/completions, where that model answers 500. "
                f"Choose a non-GPT-5 model, or one on another provider."
            )


def check_fallback_model(cfg: dict) -> None:
    """Refuse a fallback that cannot rescue anything.

    `assistant.models.fallback` is the model every hosted role swings onto when
    the one it asked for keeps erroring. Two ways to write one that looks
    configured and is not, and neither shows up until the outage it exists for:

    A fallback **already in use** rescues nothing on that route: the retry
    ladder exhausts against a model, and the fallback is the same model again.
    Name *and* provider, because the same model served by two hosts is two
    models for this purpose -- which is the useful case, not a corner one.
    But "in use" is per role, not per house: a local model that is `everyday`
    still rescues every hosted role when the internet is down, and that is
    the rescue a household actually needs. Refused only when every role names
    it, because then it rescues nobody.

    A fallback on another provider used to be refused here, and that refusal
    was right for the code as it stood: the swap replaced `kw["model"]` inside
    the failing provider's own client, so a name meant for somewhere else was a
    guaranteed 404 on top of an outage. `base.py` now routes such an entry
    through that provider's own client, so the refusal is gone.

    It should be gone, because the same-provider rule could not rescue the
    failure people actually have. It was built for one dead model behind a
    healthy gateway -- real, and what happened on 2026-08-23 -- but a provider
    that is *down* takes every model on it, and a second name on the same key
    is then a second identical failure. A household serving one model from one
    local engine has the sharpest version: there is no same-provider rescue to
    name at all.

    So what is left here is a warning, and it points the opposite way from the
    old rule: a fallback that shares its provider with every role cannot
    survive that provider going down.
    """
    models = ((cfg.get("assistant") or {}).get("models") or {})
    # One name, or an ordered list of them. A list is the shape that survives a
    # provider going down rather than one model on it: the house's roles now
    # sit on two local engines and a gateway, and a single rescue shares its
    # fate with whichever of the three it lives on. nanobot has taken a list
    # here since 2026-08-30 (`model_fallback` in its schema); this is the
    # config finally being able to say it.
    raw = models.get("fallback")
    entries = [str(v).strip() for v in (raw if isinstance(raw, list) else [raw])
               if str(v or "").strip()]
    if not entries:
        return
    for value in entries:
        _check_one_fallback(value, entries, models)


def _check_one_fallback(value: str, entries: list, models: dict) -> None:
    """One entry of the fallback chain, against every role and its siblings."""
    name, provider = split_model(value)
    if entries.count(value) > 1:
        raise DeployError(
            f"assistant.models.fallback names {value!r} twice.\n    The "
            f"second attempt is the first one again; drop it."
        )
    # `vision` and the `image_*` slots are excluded because none of them is on
    # the rescue path. Vision is selected per call for a photo and is the one
    # role routinely pointed at a local model; the image slots are generation,
    # a different API on a different provider entirely. Counting them made the
    # stranded-role warning name `image_high` and `image_normal` as things that
    # "will have no rescue", which is true of every household and means
    # nothing -- a warning that always fires is one people learn to scroll past.
    roles = {role: split_model(v) for role, v in models.items()
             if role not in ("fallback", "vision", "titles", "documents")
             and not role.startswith("image_")
             and str(v or "").strip()}
    serving = {p for _, p in roles.values()}
    same = [role for role, (n, p) in roles.items() if (n, p) == (name, provider)]
    # A fallback some roles already name still rescues the rest. Since
    # 2026-09-10 the house's everyday model IS the local one, and the same
    # local model is the only thing that keeps `powerful`, `programmer` and
    # the specialists answering when the internet does not. Refusing it here
    # would leave those roles with no offline rescue at all, to protect a
    # second attempt on the everyday route that costs nothing. So the refusal
    # is kept only for the entry that rescues nobody: every role names it.
    if same and len(same) == len(roles):
        raise DeployError(
            f"assistant.models.fallback is {value!r}, which is already "
            f"every role in assistant.models.\n    A fallback that is the "
            f"model it rescues is a second attempt at what just failed. "
            f"Choose a model no other role names."
        )
    # Only when the *whole chain* is stuck on one provider. Warning per entry
    # would fire once per candidate on a chain that is fine, and a warning that
    # repeats is one people learn to scroll past.
    chain_providers = {split_model(v)[1] for v in entries}
    if serving and serving == {provider} and chain_providers == {provider} \
            and value == entries[0]:
        out.warn(
            f"every fallback runs on {provider} and so does every role, so a "
            f"provider outage takes the rescue with it. Naming one on another "
            f"provider is what survives that."
        )


def derive(cfg: dict, secrets: dict) -> dict:
    """Config plus the values the manifest interpolates but nobody types.

    `interpolate` walks the config literally, so anything the manifest wants to
    write as `{derived.x}` has to be in it by the time a unit is read.

    Every endpoint is exported whether or not a model names it, and the ones
    that are off get an empty string rather than being absent: nanobot reads
    its whole config for `${VAR}` and refuses to start on an *unset* reference,
    so a missing name is a container that never comes up.
    """
    add_container_addresses(cfg)
    fill_blank_dns_names(cfg)
    alias_alfred_mcp_port(cfg)
    endpoints = ollama_endpoints(cfg, secrets)
    check_models_reachable(cfg, enabled_providers(cfg, secrets, endpoints))
    check_fallback_model(cfg)
    check_direct_models(cfg)

    local_url, local_key = endpoints.get("ollama", ("", "ollama"))
    # The two models on the Models page that nanobot never reads. Resolved to
    # a URL and key here because their consumers take exactly that: the
    # portal's titler (CHAT_TITLE_MODEL/URL/KEY, on the host's network) and
    # Paperless's AI (PAPERLESS_AI_LLM_*). Blank, or a
    # provider that is off, exports empty strings, and each compose file's
    # `:-` default then keeps what that service did before.
    _models = (cfg.get("assistant") or {}).get("models") or {}
    title_name, title_base, title_key = model_endpoint(
        cfg, secrets, str(_models.get("titles") or ""), host_network=True)
    docs_name, docs_base, docs_key = model_endpoint(
        cfg, secrets, str(_models.get("documents") or ""))
    # A household's own Ollama gets Paperless's Ollama client: structured JSON
    # with thinking off, against the server's root. Anything else is Paperless's
    # OpenAI-compatible client, which answers through a tool call -- a model
    # that cannot call tools (minicpm-v could not) never answers there at all.
    docs_provider = split_model(str(_models.get("documents") or ""))[1]
    docs_native = ollama_instances.is_local_provider(docs_provider)
    # `services.home-paperless.embeddings`: what Paperless indexes documents
    # with. Without it the model classifying a document never sees the
    # household's own tags, types and correspondents and invents new names for
    # everything -- measured 2026-09-12: ~60 invented names across 21
    # documents, one match with the existing taxonomy. Only meaningful while
    # the AI is on at all.
    emb_value = str(((cfg.get("services") or {}).get("home-paperless") or {})
                    .get("embeddings") or "")
    emb_name, emb_base, _emb_key = model_endpoint(cfg, secrets, emb_value)
    emb_provider = split_model(emb_value)[1]
    emb_native = ollama_instances.is_local_provider(emb_provider)
    if emb_base and not emb_native and emb_provider != docs_provider:
        # Paperless sends the documents model's key with an OpenAI-compatible
        # embedding request; it has no key of its own for embeddings.
        raise DeployError(
            f"services.home-paperless.embeddings is {emb_value!r}, on another "
            f"hosted provider than assistant.models.documents.\n"
            f"    Paperless has one API key for both. Use a local Ollama model "
            f"or the documents model's provider.")
    emb_on = bool(emb_base and docs_base)
    cloud_url, cloud_key = endpoints.get("ollama_cloud", ("", ""))
    compat_url, compat_key = openai_compatible_endpoint(cfg, secrets)
    ft_url, ft_key = freetoken_endpoint(cfg, secrets)
    # Called for its validation as much as its value: a `cloud.opencode` that
    # is switched on with nobody to serve is a Programmer space that fails at
    # the first question somebody asks it, and this turns that into a refused
    # deploy while somebody is looking.
    opencode_url = opencode_servers(cfg)
    budget = ((cfg.get("assistant") or {}).get("spend_budget") or {})
    cfg["derived"] = {
        # What the household has decided it wants to spend, per trailing
        # window, in dollars. Derived rather than interpolated straight out of
        # the config because `interpolate` hard-fails on a key that is not
        # there, and this block is new: every install that predates it would
        # stop deploying over a setting it has never been offered.
        #
        # Empty is the default and it means "no ceiling", which is the honest
        # default for a pay-as-you-go endpoint. OpenCode Zen publishes no
        # allowance to compare against -- the $12/$30/$60 the stats page used
        # to draw were the flat Go plan's ceilings, and carrying them over
        # would have the page keep citing a plan this stack no longer uses.
        # A number here is the household's own budget and nobody else's, which
        # is the same reason `dns:` ships empty.
        "usage_budget_5h": str(budget.get("per_5h") or ""),
        "usage_budget_week": str(budget.get("per_week") or ""),
        "usage_budget_month": str(budget.get("per_month") or ""),
        # Where a service's tree is rsynced. Exposed because the admin unit has
        # to mount it at the same path inside its container as out: it writes a
        # tree here and compose then bind-mounts paths out of it, and compose
        # resolves those against the host. Same string on both sides or the
        # deploy writes one directory and the container mounts another.
        "deploy_root": os.environ.get("HOME_STACK_DEPLOY_ROOT")
                       or os.path.expanduser("~/.local/share/home-stack"),
        # Who the admin container runs as, and which group reaches the docker
        # socket. Read from the deploying account rather than configured: the
        # point is that the container is *this* user, so anything it writes is
        # owned the same way a deploy from this shell would own it.
        # Who lives here, for the portal. It keys folders, chore editing and
        # the adult-only pages on the *login* id, and only users.json knows
        # which login is which member -- so these three say what a member is
        # and the portal joins them. Written down in app.py before this, as
        # five login ids from one household.
        "members": ",".join(
            str(m.get("id") or "").strip() for m in (cfg.get("members") or [])
            if str(m.get("id") or "").strip()),
        "admin_members": ",".join(
            str(m.get("id") or "").strip() for m in (cfg.get("members") or [])
            if str(m.get("id") or "").strip() and m.get("admin")),
        "member_folders": member_share_folders(cfg),
        # Who signs in as whom, and which of them are parents. Generic facts
        # about the household, exported so a household's own service can be
        # told them instead of carrying a copy.
        # The hub as a browser can reach it. `hosts.hub.address` is 127.0.0.1
        # on a single-box install, which is correct for a container dialling
        # the host and useless in a link somebody taps on a phone -- and a
        # plugin's `tiles:` are links. The core's own tiles already go through
        # browsable_address(); this is how a household's can.
        "hub_address": browsable_address(cfg, "hub"),
        # Where the household's file share actually is.
        #
        # This used to be `hosts.storage`, and that conflated two things: the
        # `storage` *role* says which machine a service is deployed onto, and
        # the share is a resource that may be on a machine this stack does not
        # deploy to at all. On the house this was extracted from they are
        # different boxes -- paperless runs on the hub and the share is on
        # another NAS -- so pointing every consumer at the role sent them to a
        # partial copy: the per-member folders were there, `finanzas/` was not,
        # and the finance dashboard answered "no months" for everybody rather
        # than failing.
        #
        # Unset, it is the storage role, which is what every existing install
        # meant by it.
        "share_host": (str((cfg.get("share") or {}).get("host") or "").strip()
                       or str(((cfg.get("hosts") or {}).get("storage") or {})
                              .get("address") or "127.0.0.1")),
        "share_host_from_container": (
            str((cfg.get("share") or {}).get("host") or "").strip()
            or (((cfg.get("hosts") or {}).get("storage") or {})
                .get("from_container")
                or str(((cfg.get("hosts") or {}).get("storage") or {})
                       .get("address") or "127.0.0.1"))),
        # Where the voice gateway finds services/audio-cpp, and **empty unless
        # that service is switched on**. The gateway offers the engine only
        # when this is set, so an address here for a container nobody started
        # would put a choice in the dropdown that fails when taken -- the same
        # rule QWEN_TTS_URL follows next to it. `_synthesize_audiocpp` raises
        # NotConfigured on empty, which is deliberately not a fallback: "no
        # audio.cpp" and "audio.cpp is restarting" are opposite problems.
        #
        # **The host-facing address and the published port, not
        # `http://audio-cpp:8080`.** That was the first answer here and it is
        # the right one for a bridge-networked caller -- which the voice
        # gateway is not: it runs `network_mode: host`, where a container name
        # does not resolve at all. The symptom is worth recognising, because it
        # is not the one an empty value gives: the roster stops saying "not
        # configured" and starts saying "not responding: Cannot connect to host
        # audio-cpp", which reads like the service being down rather than the
        # address being unreachable from this particular caller.
        #
        # Same two-right-answers problem as `ollama_url_host` above, solved the
        # same way. If a bridge-networked caller ever needs this, it gets a
        # second derived name rather than this one changing shape.
        "audiocpp_url": _audiocpp_url(cfg),
        # Alfred's voice, with defaults, because these keys arrived after the
        # config file did. `piper` is the safe engine to fall back on: it is
        # inside the gateway's own image, so it is the one choice that cannot
        # be unavailable. An empty voice means the engine's own default.
        "tts_engine": (str(((cfg.get("services") or {}).get("home-voice") or {})
                           .get("tts_engine") or "piper")),
        "tts_voice": (str(((cfg.get("services") or {}).get("home-voice") or {})
                          .get("tts_voice") or "")),
        # Which audio.cpp package speaks. A package is a file and a voice is a
        # style inside it; both are settings and neither substitutes for the
        # other. Falls back to the first package the service serves, so a
        # household that never chose one still gets something that exists.
        "tts_model": _audiocpp_model(cfg),
        # The package lists, with defaults. A config that never named them --
        # or one the admin page emptied by removing the last of a kind --
        # still deploys, because a speech runtime with no ASR is a container
        # that starts and refuses half its requests, and that is a worse
        # answer than a default.
        "tts_packages": _audiocpp_list(cfg, "tts_packages", "supertonic_3_q8_0"),
        "asr_packages": _audiocpp_list(cfg, "asr_packages", "qwen3_asr_0_6b_q8_0"),
        "member_logins": member_logins(cfg),
        # Which language each person reads the portal in. See member_locales().
        "member_locales": member_locales(cfg),
        # What Paperless reads a scan as. See ocr_language().
        "ocr_language": ocr_language(cfg),
        # The languages this house ships, as the portal's own env expects them:
        # a comma list. `locale.available` is a YAML list, and interpolating one
        # straight in gives `['en', 'es']`, which the portal would split on the
        # comma into `['en'` and `'es']` and match nothing.
        "locales_available": ",".join(
            str(x).strip() for x in ((cfg.get("locale") or {}).get("available") or [])
            if str(x).strip()),
        "admin_logins": admin_logins(cfg),
        # ntfy's own base-url -- what it stamps into the attachment and click
        # links it *delivers*, which open on the phones the notifications
        # arrive on. It was `{dns.ntfy}`, a second place to say where ntfy is
        # when `cloud.notifications` already says it, and on this house it was
        # a name nothing resolved: every link ntfy handed out was unopenable.
        # An address needs no DNS at all, which is the whole point for a link
        # somebody taps on a phone.
        "ntfy_self_url": (
            f"http://{browsable_address(cfg, 'hub')}:"
            f"{((cfg.get('services') or {}).get('ntfy') or {}).get('port', 21031)}"),
        # What n8n stamps into the webhook URLs it hands out -- addresses you
        # paste into whatever is going to call them, from wherever that is.
        # Same reasoning as ntfy's: a name only this machine resolves is not
        # an address you can give away, and the literal that was there pointed
        # at a subnet the house left behind.
        "n8n_self_url": (
            f"http://{browsable_address(cfg, 'hub')}:"
            f"{((cfg.get('services') or {}).get('n8n') or {}).get('port', 21061)}"),
        "n8n_self_host": browsable_address(cfg, "hub"),
        "ntfy_url": notification_urls(cfg)[0],
        "ntfy_url_from_container": notification_urls(cfg)[1],
        "deploy_uid": str(os.getuid()),
        "deploy_gid": str(os.getgid()),
        "docker_gid": docker_socket_gid(),
        "openai_compatible_url": compat_url,
        # Never empty, for the reason above: nanobot refuses to start on an
        # unset ${VAR}, so a switched-off provider gets a placeholder that
        # cannot authenticate rather than nothing.
        "openai_compatible_api_key": compat_key or "disabled",
        "freetoken_url": ft_url,
        # The second ollama, for everything that looks at an image: the
        # assistant's vision role, the camera clip reviewer and Paperless's
        # built-in AI. It exists because OLLAMA_NUM_PARALLEL and
        # OLLAMA_CONTEXT_LENGTH are server-global, so one endpoint cannot give
        # the text roles several small fast slots and image work a slot with a
        # window of its own.
        #
        # Falls back to the main ollama when it is switched off, rather than to
        # an empty string. Three services read this, and their compose files
        # default only on an *absent* name -- an empty one would reach them as
        # an empty endpoint and fail at the first image instead of quietly
        # doing what they did before.
        # The text instance's window, so a consumer that names a window of its
        # own (describe_image on the native endpoint) names *that* one. Ollama
        # reloads a model whose requested context differs from the one it
        # holds: asking the 65536 server for 8192 would reload gemma on every
        # image and evict the assistants' resident slots. Empty when the
        # config does not say, and the consumer keeps its own default.
        "ollama_local_context": ollama_context(cfg, "ollama"),
        # Where the vision role's model is served: the camera wall's clip
        # reviewer (CLIP_REVIEW_MODEL is that model) and nanobot's vision
        # block call it here. A setup (`ollama-media:`) is its own server,
        # and a reviewer sent to the main one would load the model there too.
        "ollama_vision_url": _vision_url(cfg, endpoints, local_url),
        # A placeholder rather than an empty string, for the reason stated at
        # the top of derive(): nanobot reads its whole config for `${...}` and
        # refuses to start on an unset reference, and a local engine that takes
        # no key still has to have the variable exist.
        "freetoken_api_key": ft_key or "disabled",
        # Where `opencode serve` listens, for HomeCore -- which is the
        # only consumer, because the Programmer space is the only thing
        # that talks to it. Empty when the household has not switched it
        # on, and empty is the honest value: the compose default beside it
        # must stay a working address, per the env-contract rule that an
        # export must never be shorter than the fallback it replaces.
        #
        # No key travels with it. opencode authenticates itself from its
        # own auth.json on the host; see opencode_servers().
        # `<member>=<url>` pairs, for HomeCore -- the only consumer, because
        # the Programmer space is the only thing that talks to opencode. Empty
        # when the household has not switched it on, and empty is the honest
        # value: the space then falls back to the assistant.
        #
        # No credential travels with it. opencode authenticates itself from its
        # own auth.json, and the token that opens each member's bridge is
        # written into that member's own opencode config.
        # `v1` or `v2`. Validated rather than passed through: a typo here
        # would silently mean v1, and the difference is visible to whoever
        # is typing -- v2 answers in one piece where v1 streams.
        "opencode_api": opencode_api(cfg),
        "opencode_servers": ",".join(
            f"{m}={u}" for m, u in sorted(opencode_url.items())),
        # The bare name of `assistant.models.vision`, for consumers that talk
        # to Ollama directly rather than through nanobot. `split_model` strips
        # the `ollama:` prefix; the provider is not carried because the only
        # consumer today is the camera reviewer, which has one endpoint.
        #
        # Empty when the household names no vision model, and the compose
        # default must therefore still be a working one -- an export that
        # beats a correct fallback with an empty string is the bug the env
        # contract section of CLAUDE.md is about.
        "vision_model": split_model(
            ((cfg.get("assistant") or {}).get("models") or {}).get("vision", ""))[0],
        # `assistant.models.titles`, for home-core. The URL is the full chat
        # completions endpoint because that is what the titler POSTs to.
        "title_model": title_name if title_base else "",
        "title_url": f"{title_base}/chat/completions" if title_base else "",
        "title_key": title_key if title_base else "",
        # `assistant.models.documents`, for Paperless's AI -- under the names
        # Paperless 3.x reads (PAPERLESS_AI_LLM_*). The stack exported
        # PAPERLESS_AI_MODEL/ENDPOINT/API_KEY/PROVIDER until 2026-09-12, which
        # nothing reads, so the AI was never configured and never switched on.
        # On exactly when a model is chosen; blank turns it off.
        "paperless_enabled": "true" if docs_base else "false",
        "paperless_backend": ("ollama" if docs_native else "openai-like") if docs_base else "",
        "paperless_model": docs_name if docs_base else "",
        "paperless_endpoint": (docs_base[:-len("/v1")]
                               if docs_native and docs_base.endswith("/v1") else docs_base),
        "paperless_key": docs_key if docs_base else "",
        # Only a local Ollama needs it, and only if the config says it; see
        # ollama_context(). Empty keeps Paperless's own 8192.
        "paperless_context": ollama_context(cfg, docs_provider) if docs_base and docs_native else "",
        # What its suggestions are written in. Empty falls back to each user's
        # UI language, which for a document nobody has opened yet is nobody's.
        "paperless_output_language": str((cfg.get("locale") or {}).get("default") or ""),
        # The index. Empty means absent: the unit lists all three under
        # `env_omit_empty`, because Paperless refuses to start on an empty
        # embedding backend.
        "paperless_embedding_backend": ("ollama" if emb_native else "openai-like") if emb_on else "",
        "paperless_embedding_model": emb_name if emb_on else "",
        "paperless_embedding_endpoint": (emb_base[:-len("/v1")]
                                         if emb_on and emb_native and emb_base.endswith("/v1")
                                         else (emb_base if emb_on else "")),
        # Whether the camera wall reviews its clips at all, as the literal the
        # reviewer parses rather than as a YAML bool.
        #
        # `interpolate()` renders a value with `str()`, so `false` arrives as
        # the string "False" -- and clip_review.py asks `not in ('0', 'false',
        # '')`, which "False" is not. The switch would have read as ON, which
        # is the direction that costs a card the household had just freed.
        #
        # Default on: a household that has never heard of this setting keeps
        # the reviewer it already had. Off means keep, never purge -- see the
        # `REVIEW_ENABLED` branch in clip_review.py.
        "clip_review_enabled": (
            "0" if ((cfg.get("services") or {}).get("home-cameras") or {})
            .get("clip_review", True) is False else "1"),
        # A token per reporting container, matching `_service_token()` in
        # HomeCore. Empty when the household has no master secret, which
        # switches the reporting off rather than sending an empty header --
        # the receiving end refuses those, so an unconfigured install records
        # nothing instead of recording it as an anonymous caller.
        "usage_token_voice": (
            derive_service_token(secrets["PROXY_SHARED_SECRET"], "voice-gateway")
            if secrets.get("PROXY_SHARED_SECRET") else ""),
        "usage_token_cameras": (
            derive_service_token(secrets["PROXY_SHARED_SECRET"], "home-cameras")
            if secrets.get("PROXY_SHARED_SECRET") else ""),
        # Whatever GPU exporter the household already runs, or empty. See the
        # note in the example config for why this stack does not ship one.
        "gpu_exporter_url": str(
            (cfg.get("site") or {}).get("gpu_exporter", "") or "").strip(),
        "ollama_url": local_url,
        "ollama_api_key": local_key,
        "ollama_cloud_url": cloud_url,
        # Never empty, for the reason above. A placeholder that cannot
        # authenticate is the honest value when the provider is switched off.
        "ollama_cloud_api_key": cloud_key or "disabled",
        "house_only_apps": house_only_apps(cfg),
        "home_networks": home_networks(cfg),
        # For a consumer on host networking. See ollama_endpoints().
        "ollama_url_host": endpoints.get("ollama_host", ("", ""))[0],
    }
    return cfg


# What a *container* dials to reach a service on the host. Every role points at
# 127.0.0.1 on the default one-PC install, and inside a bridge-networked
# container that is the container itself -- so a URL built from `.address` is
# handed to the container as its own loopback and connects to nothing.
#
# Proved rather than reasoned about: from inside nanobot-user1 on a default
# install, `http://127.0.0.1:8010/skill` (Paperless) and
# `https://127.0.0.1:8443/api/tasks` (the portal) both answer "Connection
# refused", while the same request to `host.docker.internal` is reachable. It
# fails silently, because a skill that cannot reach its service just reports
# the service as down.
#
# `host.docker.internal` needs `extra_hosts: host.docker.internal:host-gateway`
# on the service that dials -- not on the unit, not on a neighbour, and not in
# a comment mentioning it. `loopback_reachability_problems` checks the parsed
# `extra_hosts:` of each service, and test_deploy.py asserts all three of those
# distinctions plus the shipped package as a whole.
#
# It also needs the service on the other end to be listening on something other
# than the host's own loopback. That is true of everything this package deploys
# (each publishes a port), and it is the one thing to check by hand for an
# Ollama you run yourself: stock `ollama serve` binds 127.0.0.1, so a container
# reaching the gateway address needs OLLAMA_HOST=0.0.0.0 set on it.
CONTAINER_GATEWAY = "host.docker.internal"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# Which portal apps a service is reachable through, for `house_only:`. Only
# services the portal actually proxies can be kept to the house -- everything
# else is reachable exactly as far as its port is published, which is a
# different question and a different setting.
HOUSE_ONLY_APPS = {"home-cameras": "cameras"}


# Which host each `dns:` name stands in front of, so a name left blank can fall
# back to that host's address. Names not listed fall back to the hub.
DNS_FALLBACK_HOST = {
    "cameras": "compute",
    "voice": "compute",
    # faster-whisper runs on `compute`, not the hub. Missing from this table,
    # a blank `dns.whisper` fell back to the hub's address -- so the portal and
    # the voice gateway posted their audio at a box with no transcriber on it,
    # and the admin page's DNS table told the household to point the name there
    # too.
    "whisper": "compute",
    "paperless": "storage",
}
# `ntfy` was here and is not a `dns:` name any more: where notifications go is
# `cloud.notifications`, and the one thing that needed an address -- the
# base-url ntfy stamps into the links it delivers -- takes the hub's address
# directly. Two places to say where ntfy is meant one of them was always
# capable of being wrong, and on the house this came from it was.

# And which service answers each one. A name is only a name -- nothing in this
# package resolves them -- so a row for a service the household does not run is
# a line somebody copies into their resolver for an address where nothing is
# listening.
#
# `ntfy` is the one that prompted this: switch `cloud.notifications.mode` to
# `external` and the ntfy service is not deployed at all, while the admin page
# went on offering a name for it beside the ones that work.
DNS_SERVICE = {
    "portal": "home-core",
    "chat": "home-core",
    # opencode's interface: served by home-core's Caddy, and the
    # handoff that authorises it is home-core's too.
    "code": "home-core",
    "cameras": "home-cameras",
    "mqtt": "mqtt",
    "paperless": "home-paperless",
    "assistant": "nanobot-house",
    "voice": "home-voice",
    "whisper": "faster-whisper",
}


def _own_addresses() -> set[str]:
    """Every address that means "this machine", loopback included."""
    own = set(_LOOPBACK)
    try:
        own.update(subprocess.run(["hostname", "-I"], capture_output=True,
                                  text=True, timeout=5).stdout.split())
    except Exception:  # noqa: BLE001 - a box without `hostname` still deploys
        pass
    return own


def check_dns_points_at_its_service(cfg: dict, manifest: dict) -> None:
    """A `dns:` name has to resolve to the machine that runs the service behind it.

    Every check this deployer had asked whether a value was *present*.
    `--check-contract` asks whether compose gets every variable, the health
    checks ask whether a container answers, the certificate check asks whether
    a name is served. None of them asks whether the name points at the right
    box, so a name that is real, resolvable and belonging to a different
    machine passes all of them.

    That is not hypothetical. `dns.assistant` was edited from `assistant.home`
    to `homeassistant.home` -- one word apart in a list containing both -- and
    the manifest exports `NANOBOT_HOST: "{dns.assistant}"`. HomeCore then
    looked for the assistant on the Home Assistant box, which has never run
    one. The deploy went green; the household got
    `Connection refused ... homeassistant.home:21301` on every message, six
    hours after the edit, because a container keeps the environment it was
    built with and nothing redeployed home-core until then.

    A name that does not resolve is a *warning*, not a failure. Plenty of
    households have no local DNS -- that is the whole reason `dns:` may be left
    empty -- and this also runs from inside the admin container, where `.home`
    names are NXDOMAIN. Refusing there would break deploying from the page.
    Resolving to the *wrong* machine is different: somebody wrote a name, it
    answers, and it answers about somewhere else.
    """
    dns = cfg.get("dns") or {}
    hosts = cfg.get("hosts") or {}
    site = cfg.get("services") or {}
    specs = manifest.get("services") or {}
    own = _own_addresses()

    for key, service in sorted(DNS_SERVICE.items()):
        name = str(dns.get(key) or "").strip()
        if not name:
            continue
        try:
            ipaddress.ip_address(name)
            continue          # fill_blank_dns_names writes addresses, not names
        except ValueError:
            pass
        svc_cfg = site.get(service) or {}
        if not svc_cfg.get("enabled", True):
            continue
        role = (specs.get(service) or {}).get("role") or svc_cfg.get("host")
        expected = str((hosts.get(role) or {}).get("address") or "").strip()
        if not expected:
            continue
        try:
            got = {info[4][0] for info in
                   socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)}
        except (socket.gaierror, UnicodeError, OSError):
            out.warn(f"dns.{key} is {name!r}, which does not resolve here. "
                     f"{service} is reached by that name, so nothing will find "
                     f"it unless your resolver answers it.")
            continue
        # A role on loopback means "this machine", and the name for it will
        # resolve to the LAN address rather than to 127.0.0.1. Comparing the
        # literal would fail every correctly-configured single-PC install.
        wanted = own if (expected in _LOOPBACK or expected in own) else {expected}
        if got & wanted:
            continue
        raise DeployError(
            f"dns.{key} is {name!r}, which resolves to "
            f"{', '.join(sorted(got))} -- but {service} runs on "
            f"hosts.{role} ({expected}).\n"
            f"    Whatever is at that address, it is not this stack's "
            f"{service}, and every request sent to the name will go there.\n"
            f"    Point dns.{key} at the machine that runs {service}, or clear "
            f"it to use the address directly."
        )


def fill_blank_dns_names(cfg: dict) -> None:
    """Let every `dns:` name be left empty, or missing entirely.

    These are the names people type, and they only work if something on the
    network answers them -- a Pi-hole, a router that does local DNS, or a hosts
    file. Plenty of households have none of that, and for them the honest
    answer is an address, not a name that never resolves.

    So a blank name becomes the address of the host it stands in front of. The
    stack works either way: no internal URL in this package resolves a name,
    because a container retrying `getaddrinfo ENOTFOUND` forever looks exactly
    like the service being down. What a blank name costs is a nice URL, not a
    working house.
    """
    dns = cfg.setdefault("dns", {})
    if not isinstance(dns, dict):
        return
    hosts = cfg.get("hosts") or {}
    # Every name this deployer knows about, not only the ones the config
    # happens to list. A name added to the manifest after a household wrote its
    # config is *absent* rather than blank, and `interpolate()` raises on a path
    # the config has not got -- so `dns.whisper` stopped every existing install
    # from deploying the portal and the voice gateway, with an error about a key
    # their own config had no reason to carry. Filling it here is the same
    # answer CONFIG_DEFAULTS gives a new setting, said once for the whole block
    # rather than once per name somebody remembers to add.
    for key in DNS_SERVICE:
        dns.setdefault(key, "")
    for key, value in list(dns.items()):
        if str(value or "").strip():
            continue
        role = DNS_FALLBACK_HOST.get(key, "hub")
        host = hosts.get(role) or hosts.get("hub") or {}
        address = str(host.get("address") or "").strip()
        if address:
            dns[key] = address


def build_targets(cfg: dict, dry_run: bool = False) -> dict:
    """One Target per role this stack can reach.

    The roles under `hosts:`, plus `vps` -- which is not one of them. The VPS's
    address lives under `cloud.vps` because it is not a role a household
    assigns services to: it runs exactly one proxy and holds no application
    code. That is a good reason for it to be somewhere else in the config and
    no reason at all for it to be missing here.

    One copy, because there were two. The deployer built the vps target and
    backup.py did not, so a household with `cloud.vps.enabled` got
    `KeyError: 'vps'` out of `./home-stack backup` -- from the line collecting
    which roles are remote, before anything was copied.
    """
    targets = {role: Target(role, host_cfg, dry_run)
               for role, host_cfg in (cfg.get("hosts") or {}).items()}
    vps = (cfg.get("cloud", {}).get("vps") or {})
    if vps.get("enabled") and vps.get("host"):
        targets["vps"] = Target("vps", {"address": vps["host"],
                                        "user": vps.get("user") or "root"},
                                dry_run)
    return targets


def home_networks(cfg: dict) -> str:
    """`HOME_NETWORKS` for the VPS copy: which addresses count as inside.

    The Android app hard-codes the public URL, so it always reaches the public
    copy and can never be recognised the way a LAN browser is. What tells it
    apart is the VPN, and the proxy defaults to Tailscale's range on its own --
    so this is empty unless the household adds to it.

    Explicitly *not* the household's public address. That would show the
    house-only links on the home wifi with no VPN at all, which is not what
    "LAN or VPN" means: it reads "same building" as "came in privately", it
    breaks silently when the ISP changes the address, and behind CGNAT it is
    not even the same household.

    Empty is not "nobody": the compose file carries the real default and an
    empty export falls through to it.
    """
    vps = ((cfg.get("cloud") or {}).get("vps") or {})
    nets = vps.get("home_networks") or []
    if isinstance(nets, str):
        nets = nets.split(",")
    return ",".join(str(n).strip() for n in nets if str(n).strip())


def house_only_apps(cfg: dict) -> str:
    """The `HOUSE_ONLY_APPS` list, as the portal and the proxy read it.

    A service marked `house_only: true` is served only to a browser on the LAN
    or over the VPN -- the portal refuses the path and the public copy of the
    proxy does not route the prefix at all. Two gates on purpose: the outer one
    means the prefix is not served from a public host, and the inner one means
    it is still refused if anything ever reaches past it.

    Default true for the cameras, which is the behaviour this shipped with. A
    camera feed behind a login on a public host is not the boundary most people
    want, but it is the household's call, and saying so should not mean
    patching the portal.
    """
    services = cfg.get("services") or {}
    keys = []
    for service, app_key in sorted(HOUSE_ONLY_APPS.items()):
        conf = services.get(service) or {}
        if conf.get("house_only", True):
            keys.append(app_key)
    return ",".join(keys)


def add_container_addresses(cfg: dict) -> None:
    """Give every host role a `from_container` address beside its `address`.

    Two names because there are genuinely two answers. `address` is right for
    anything running on the host -- ssh, rsync, a verify `curl` -- and for a
    service on host networking. `from_container` is right for a bridge-networked
    container dialing out. When the address is not loopback they are the same
    string, which is why this is invisible on a split install.

    `setdefault`, like `apply_config_defaults`: the manifest interpolates
    `{hosts.<role>.from_container}` and the contract check names it in its own
    error message, so it reads like an ordinary site setting -- and a household
    whose containers must reach a role some other way (rootless Podman, a
    daemon without host-gateway, a fixed bridge address) can write one and have
    it survive the next deploy, instead of being silently overwritten.
    """
    for host in (cfg.get("hosts") or {}).values():
        if not isinstance(host, dict):
            continue
        address = str(host.get("address") or "").strip()
        if not address:
            # A role with no address at all is left alone on purpose. Writing
            # `from_container: ""` here would turn a missing setting into a URL
            # with no host in it -- `http://:8010` -- that `--check-contract`
            # calls fine and a container fails on at runtime. Left unset,
            # `interpolate` still refuses the deploy and names the role.
            # admin/app.py creates exactly this shape: it does
            # `hosts.setdefault(role, {})` for all three roles and only assigns
            # `address` when the form field was filled in.
            continue
        host.setdefault("from_container",
                        CONTAINER_GATEWAY if address in _LOOPBACK else address)


def member_ids(cfg: dict) -> list[str]:
    return list(cfg.get("services", {}).get("nanobot", {}).get("members", []))


def member_env_suffix(member: str) -> str:
    """`user1` -> `USER_1`.

    The underscore is deliberate: NANOBOT_API_SECRET_USER1 read as one mashed
    word, while USER_1 reads as what it is -- a key scoped to an id. The member
    id itself stays `user1` (it is woven through paths, topics and fixtures);
    only the environment-variable suffix carries the separator.
    """
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", member)
    return f"{m.group(1)}_{m.group(2)}".upper() if m else member.upper()


def expand_per_member(keys: list[str], members: list[str]) -> list[str]:
    """`NANOBOT_API_SECRET_{M}` over [user1, user2] -> the two real key names."""
    expanded = []
    for key in keys:
        for member in members:
            expanded.append(key.replace("{M}", member_env_suffix(member)))
    return expanded


# --------------------------------------------------------------------------
# Secret checks
# --------------------------------------------------------------------------

def _audiocpp_list(cfg: dict, key: str, fallback: str) -> str:
    """One of audio.cpp's package lists, never empty.

    `none` is the one deliberate empty: a household that transcribes with
    faster-whisper has no use for an ASR package in audio.cpp, and until
    2026-09-21 the only way to say so was to leave a 1.15 GB model loaded on
    the card. The word travels as-is -- compose's `${VAR:-default}` would
    re-fill a blank -- and the entrypoint turns it into no entries.
    """
    svc = (cfg.get("services") or {}).get("audio-cpp") or {}
    listed = [p.strip() for p in str(svc.get(key) or "").split(",") if p.strip()]
    return ",".join(listed) if listed else fallback


def _audiocpp_model(cfg: dict) -> str:
    """The audio.cpp package the gateway asks for, with a working default."""
    svc = (cfg.get("services") or {}).get("audio-cpp") or {}
    chosen = str(svc.get("tts_model") or "").strip()
    served = [p.strip() for p in str(svc.get("tts_packages") or "").split(",")
              if p.strip()]
    # A chosen package that is no longer served would export a name the
    # container never installed, and the first request would 404. The served
    # list wins over a stale choice.
    if chosen and chosen in served:
        return chosen
    return served[0] if served else "supertonic_3_q8_0"


def _audiocpp_url(cfg: dict) -> str:
    """The audio.cpp base URL as the *host* sees it, or "" when it is off.

    Its compose publishes on `127.0.0.1:<port>` only, and its one consumer --
    the voice gateway -- shares the host's network namespace, so loopback is
    exactly what it needs and a container name is exactly what it cannot use.
    """
    svc = (cfg.get("services") or {}).get("audio-cpp") or {}
    if not svc.get("enabled", True):
        return ""
    role = str(svc.get("host") or "compute")
    address = str((((cfg.get("hosts") or {}).get(role)) or {})
                  .get("address") or "127.0.0.1")
    return f"http://{address}:{svc.get('port', 21014)}"


def derive_proxy_token(shared_secret: str, login: str) -> str:
    """The portal's per-person task-API token.

    Keyed on the **login id**, because that is what the portal hashes:
    `_proxy_user_token()` there is `sha256(f"{secret}:{username}")` over the
    name in users.json, and `_proxy_auth` looks the same name up with
    `find_user()` before it compares anything.

    This derived it from the *member* id, so the two never matched and the
    lookup never even got that far -- measured against the live portal on
    2026-08-30: member id and its token, 401; login id and a token derived from
    the login, 200 with the household's real data; login id with a token derived
    from the member, 401 again. So both halves have to be the login, and this is
    the half that is a credential.

    Kept here as well as in install.sh because the two must agree: the portal
    computes the same value at request time, and a mismatch shows up as task
    notification buttons that 401 silently rather than as anything visible.
    """
    return hashlib.sha256(f"{shared_secret}:{login}".encode()).hexdigest()


def derive_code_broker_token(secret: str, instance: str) -> str:
    """The token one assistant instance presents to the code broker.

    Same shape as the proxy token above and for the same reason: derived rather
    than stored, so rotating the secret cannot leave a stale copy behind in the
    credentials file. Per instance, because the whole point is that an agent can
    act for its own member and for nobody else -- the broker holds the projects'
    credentials and none of the family's, and the agents the reverse.
    """
    return hashlib.sha256(f"{secret}:{instance}".encode()).hexdigest()


def derive_alfred_mcp_token(secret: str, member: str) -> str:
    """The token opencode presents to one member's MCP bridge.

    Third of the same shape -- derived from a shared secret and the member,
    never stored -- so rotating the secret cannot leave a stale copy behind.

    It is deliberately *not* the code broker's token or the proxy's. Those open
    that person's code and that person's portal account respectively; this one
    only opens the bridge. Reusing one would mean the value sitting in
    opencode.json on the host, where a person can read it, is also the value
    that talks to the broker directly.
    """
    return hashlib.sha256(f"{secret}:{member}".encode()).hexdigest()


def derive_crawl4ai_secret(shared_secret: str, purpose: str) -> str:
    """crawl4ai's token and signing key, derived rather than stored.

    Fourth of the same shape as the three above, and the one whose reason is
    different. The others are credentials that gate access to something. This
    one is barely a credential at all: crawl4ai's entrypoint reads
    `CRAWL4AI_API_TOKEN` to decide its **socket bind**, and refuses to listen
    on anything but the container's own loopback without it. So an empty value
    does not mean "no auth", it means "unreachable" -- and there is nobody for
    the household to obtain a token *from*, because the server is theirs.

    Deriving it makes that plain. There is no key to generate, none to type,
    none to lose, and none sitting in the env file to go stale when
    PROXY_SHARED_SECRET is rotated. An existing household upgrading into
    crawl4ai gets a working one on the next deploy without being told to run
    anything -- which the previous arrangement, an installer-generated key
    declared `required:`, did not: it failed the contract check on every box
    that had not re-run the installer.

    `purpose` separates the two values so the bearer token and the JWT signing
    key are not the same string. They are used by different code paths inside
    that container and one is sent over the wire on every request.
    """
    return hashlib.sha256(
        f"{shared_secret}:crawl4ai:{purpose}".encode()).hexdigest()


def derive_service_token(shared_secret: str, service: str) -> str:
    """A token that says "I am this container", not "I am this person".

    Fifth of the same shape, and the first whose subject is not a credential
    for something outside the house. The voice gateway and the camera wall
    report what the household's own models did, to HomeCore's `/stats/api/local`
    -- work that has no user, no session and no cookie behind it.

    Every other authenticated caller in this stack answers "which member is
    this", because every route that had one was reached on somebody's behalf.
    These are not, and the tempting shortcut -- lending them a member's derived
    proxy token -- would put a person's name on a row that belongs to a room,
    and hand a container that transcribes audio a credential that opens that
    person's mailbox config.

    So the namespace is separate. `svc:` can never collide with a login id,
    `find_user()` refuses it, and a leaked one buys nothing but the ability to
    write a row into a usage table. It must match `_service_token()` in
    HomeCore exactly; `test_deploy.py` pins the pair.
    """
    return hashlib.sha256(
        f"{shared_secret}:svc:{service}".encode()).hexdigest()


def collect_env(spec: dict, unit: dict, secrets: dict, cfg: dict,
                contributions: dict | None = None) -> dict[str, str]:
    """Build the complete environment one unit's compose file will interpolate.

    Three sources, in this order:

      1. The secrets the service declares -- and only those. That is what keeps
         the shared room-facing assistant from holding per-member credentials
         even when they exist in the env file.
      2. `env:` on the unit, interpolated against config/home-stack.yml. A value
         of `$OTHER_KEY` aliases a secret under the name the compose file
         actually reads.
      3. `state:` entries that name a variable, so a bind mount points at the
         absolute path the manifest declared.

    This function used to stop after (1) plus five HOME_STACK_* names, which
    meant the manifest's `state:` and port declarations were decorative: compose
    fell back to its own defaults, several of which are relative paths *inside*
    the directory the deployer rsyncs with --delete. `--check-contract` exists
    to keep that from happening again.
    """
    declared = spec.get("secrets", {}) or {}
    members = member_ids(cfg)

    required = list(declared.get("required", []))
    optional = list(declared.get("optional", []))
    per_member = expand_per_member(declared.get("per_member", []), members)

    env: dict[str, str] = {}
    missing: list[str] = []

    # Derived before the loops below, so a unit that declares these as
    # `required:` is satisfied without anything having been written to the
    # credentials file. Both the crawler and the assistants read the token, and
    # deriving it is what keeps the two ends holding the same string without a
    # copy of it existing anywhere on disk.
    if secrets.get("PROXY_SHARED_SECRET"):
        shared = secrets["PROXY_SHARED_SECRET"]
        secrets = {
            "CRAWL4AI_API_TOKEN": derive_crawl4ai_secret(shared, "token"),
            "CRAWL4AI_SECRET_KEY": derive_crawl4ai_secret(shared, "jwt"),
            # The file still wins if somebody set one by hand: this is a
            # convenience, not a policy, and an operator who wants a specific
            # token should be able to have one.
            **secrets,
        }

    for key in required:
        if key in secrets:
            env[key] = secrets[key]
        else:
            missing.append(key)

    for key in optional + per_member:
        if key in secrets:
            env[key] = secrets[key]

    # Where each member sits in the *live* list, 1-based, so the assistant can
    # space its heartbeat without knowing who else is running.
    #
    # It derives that from the digits in NANOBOT_INSTANCE otherwise, and those
    # are not a dense sequence: member ids are monotonic and never reused (see
    # CLAUDE.md -- refilling a freed slot handed a new person the departed
    # member's derived token and backups), so a household that has seen
    # departures runs `user2` beside `user15`. `frac(n * phi)` is
    # low-discrepancy over a consecutive run and says nothing about a sparse
    # one: an id gap of 13 puts two instances 62s apart, a gap of 34 puts them
    # 23.7s apart -- worse than the hashing that walk replaced, and silent,
    # because the offsets still look spread when printed.
    #
    # The rank is the deployer's to know and only the deployer's: this is the
    # process holding the member list. The house instance is not in it and
    # takes index 0, which is where an unnumbered name already lands.
    #
    # Gated on the unit asking for it. Unconditionally, this put every member's
    # id and position into the environment of every unit of every service --
    # mqtt, paperless, the cameras on another box, and `nanobot-house`, the one
    # container CLAUDE.md says must hold no per-member material and whose
    # deploy asserts exactly that afterwards. A fourth, unconditional source
    # also breaks the promise the docstring above opens with, and would have
    # made the plugin guard below reject any plugin naming one of these keys.
    # Which model draws, for a unit that says it wants to know. Declared on the
    # unit rather than exported to everything: the room-facing assistant and
    # the per-member ones both draw; nothing else does, and an export nothing
    # reads is the shape this deployer keeps being bitten by.
    if unit.get("image_models"):
        env.update(image_model_env(cfg))

    if unit.get("member_stagger_index"):
        for rank, member in enumerate(members, start=1):
            env[f"NANOBOT_STAGGER_INDEX_{member_env_suffix(member)}"] = str(rank)

    # The code broker's per-member tokens, and the instance -> portal login map
    # it checks an access list against. Gated the same way and for the same
    # reason: this is per-member material, and the unit that does not run a
    # broker has no business holding it.
    #
    # The login, from the portal's own user store -- not the member id. This
    # read `f"{m}:{m}"` on the stated assumption that "the portal username *is*
    # the member id", which is the confusion the three-ids rule exists to
    # prevent: people sign in with a number, and `find_user()` at the other end
    # takes that number. The broker asked the registry about `user1`, the portal
    # answered `usuario desconocido`, and every git verb the assistant offers
    # returned 404 -- from two services that were both running and both healthy.
    #
    # A member with no portal account is left out rather than mapped to itself.
    # There is no request they could make, and an instance missing from this map
    # is refused before any registry call -- which is the failure you want on a
    # service handing out other people's code. Same rule as member_logins().
    if unit.get("code_broker_tokens") and "CODE_BROKER_SECRET" in env:
        logins = portal_logins(cfg)
        for member in members:
            env[f"CODE_BROKER_TOKEN_{member_env_suffix(member)}"] = \
                derive_code_broker_token(env["CODE_BROKER_SECRET"], member)
        env["CODE_BROKER_USERS"] = ",".join(
            f"{m}:{logins[m]}" for m in members if logins.get(m))

    # The MCP bridge's identity: which person this container serves, in all
    # three of the ids that name them, plus the token opencode must present.
    #
    # All three, because each is right in exactly one place and none is
    # derivable from the others -- the broker and the checkout are keyed on the
    # member, every portal request on the login, and paths on the share on the
    # folder. This is the conversion CLAUDE.md says to do once, at the edge; the
    # service itself does none.
    #
    # A member with no portal account gets no token, the same rule as the
    # broker map above and for the same reason: there is no request they could
    # make, and an empty value is refused at the door rather than somewhere
    # deeper.
    if unit.get("alfred_mcp_identity"):
        # One bridge per member, so one set of these per member. Every value is
        # derived from the member id -- the three ids that name the person, and
        # the two tokens they present -- which is what keeps a per-member value
        # from being right for one member and wrong for the next.
        #
        # A member with no portal account gets nothing rather than a token for
        # a name the portal cannot find: the share and the delegate endpoint
        # are reached with a login, and an empty one is refused at the door
        # instead of somewhere deeper. Same rule as the code broker map.
        by_id = {str(m.get("id")): m for m in (cfg.get("members") or [])}
        logins = portal_logins(cfg)
        wanted = _generator(GENERATORS["alfred-mcp"]).members(cfg)
        for member in wanted:
            if member not in by_id:
                raise DeployError(
                    f"{member!r} has the Programmer switched on but is not a "
                    f"member of this household.\n"
                    f"    Known members: "
                    f"{', '.join(sorted(by_id)) or '(none)'}")
            login = logins.get(member, "")
            if not login:
                raise DeployError(
                    f"{member!r} has the Programmer switched on but has no "
                    f"portal account.\n"
                    f"    The share and the delegate endpoint are reached with "
                    f"a login; create one on the admin page first.")
            suffix = member_env_suffix(member)
            env[f"ALFRED_MCP_LOGIN_{suffix}"] = login
            if "ALFRED_MCP_SECRET" in env:
                env[f"ALFRED_MCP_TOKEN_{suffix}"] = derive_alfred_mcp_token(
                    env["ALFRED_MCP_SECRET"], member)
            if "CODE_BROKER_SECRET" in env:
                env[f"CODE_BROKER_TOKEN_{suffix}"] = derive_code_broker_token(
                    env["CODE_BROKER_SECRET"], member)
            if "PROXY_SHARED_SECRET" in env:
                env[f"HOMECORE_PROXY_TOKEN_{suffix}"] = derive_proxy_token(
                    env["PROXY_SHARED_SECRET"], login)

    # Derived tokens: computed rather than read, so rotating the shared secret
    # cannot leave a stale derivation behind in the env file.
    if "PROXY_SHARED_SECRET" in env:
        # The variable is *named* after the member -- it says which container
        # receives it -- and its value is derived from that person's login,
        # which is what the portal hashes. Two ids, one line, and they are not
        # interchangeable: see derive_proxy_token.
        #
        # A member with no portal account gets no token rather than a token for
        # a name the portal cannot find. There is no request they could make,
        # and an empty value is refused at the door instead of somewhere deeper.
        proxy_logins = portal_logins(cfg)
        for member in members:
            name = f"HOMECORE_PROXY_TOKEN_{member_env_suffix(member)}"
            if name in per_member:
                login = proxy_logins.get(member)
                env[name] = (derive_proxy_token(env["PROXY_SHARED_SECRET"], login)
                             if login else "")

    if missing:
        raise DeployError(
            "missing required secrets: " + ", ".join(missing)
            + "\n    Fill them in secrets/smart-home-bot.env, or run: "
              "./home-stack install --generate-secrets"
        )

    # What plugins add to this service, if it is one they contribute to. Last,
    # so a contribution cannot quietly displace a secret or a manifest `env:`
    # value -- a plugin extends a core service, it does not reconfigure it.
    for key, value in ((contributions or {}).get("env") or {}).items():
        if key in env:
            raise DeployError(
                f"a plugin contributes {key}, which this service already sets"
            )
        env[key] = value
    rows = (contributions or {}).get("skills") or []
    if rows:
        env["NANOBOT_SKILL_SERVICES"] = skill_services_value(rows)

    # Settings every container gets.
    env["TZ"] = cfg["site"]["timezone"]
    env["HOME_STACK_DOMAIN"] = cfg["site"]["domain"]
    env["HOME_STACK_SITE_NAME"] = cfg["site"]["name"]
    env["HOME_STACK_HOST"] = cfg["site"]["host"]
    # The portal's public address, for the pages that send a person there. The
    # PHP entry page built this itself and hardcoded `:8443` while doing it, so
    # moving `services.home-core.port` sent every visitor to a closed port --
    # from a page whose whole job is the way in.
    env["HOME_STACK_PORTAL_URL"] = (
        f"https://{cfg['dns']['portal']}:{cfg['services']['home-core']['port']}"
        if (cfg.get("dns") or {}).get("portal")
        else f"https://hub.{cfg['site']['domain']}:{cfg['services']['home-core']['port']}"
    )
    env["HOME_STACK_DEFAULT_LOCALE"] = (cfg.get("locale") or {}).get("default", "en")
    env["HOME_STACK_LOCALES"] = ",".join((cfg.get("locale") or {}).get("available", ["en"]))
    # Containers that write into a bind mount run as the deploying user rather
    # than root, or the next deploy cannot read what they wrote.
    env.setdefault("UID", str(os.getuid()))
    env.setdefault("GID", str(os.getgid()))

    # HOST_HUB / HOST_COMPUTE / HOST_STORAGE used to be exported here, under
    # the comment "so a compose file can pin extra_hosts without a literal".
    # Nothing read them, anywhere, ever: a documented capability that had never
    # worked, which is the same decorative-declaration class the `state:` names
    # are checked for. They could not have done that job either -- an
    # `extra_hosts` entry needs `host-gateway`, a value Docker resolves, not an
    # address this deployer knows.
    #
    # So the gateway alias stays a literal in the compose files that need it.
    # What keeps that honest is not indirection but
    # `loopback_reachability_problems`: a service handed the gateway name
    # without the alias fails the contract check, which is exactly the mistake
    # the two units that needed this fix had made. Parameterising the literal
    # would only hide it from that check.

    # The configured names, for the few places a person sees one. Nothing
    # internal resolves these -- see the `dns:` comment in the config.
    for alias, name in (cfg.get("dns") or {}).items():
        env[f"DNS_{alias.upper()}"] = str(name)

    # State paths that name a variable.
    for entry in interpolate(unit.get("state", []), cfg):
        if entry.get("env"):
            env[entry["env"]] = entry["path"]

    # The unit's own settings. `$NAME` aliases a secret already collected above,
    # which is how a compose file reading SECRET_KEY is fed HOMECORE_SECRET_KEY.
    for name, value in interpolate(unit.get("env", {}), cfg).items():
        value = str(value)
        if value.startswith("$"):
            alias = value[1:]
            if alias in env:
                env[name] = env[alias]
            # An absent alias is left unset rather than set to "": an empty
            # credential fails as an outage, a missing one fails as config.
        else:
            env[name] = value

    # Settings that must be absent rather than empty. A program that checks a
    # value against a list of choices refuses to start on "" -- Paperless does,
    # for PAPERLESS_AI_LLM_EMBEDDING_BACKEND -- and a compose file cannot unset
    # a variable, only default it. Named here, an empty value is dropped, and a
    # compose entry declared without one (`KEY:`) then leaves it unset.
    for name in unit.get("env_omit_empty") or []:
        if env.get(name) == "":
            del env[name]

    return env


# --------------------------------------------------------------------------
# Running things, locally or over ssh
# --------------------------------------------------------------------------

def in_container() -> bool:
    """Is this deployer running inside a container?

    Matters because `is_local` short-circuits ssh and rsync for a loopback
    address, and that is right on a host and wrong in a container. The admin
    page runs the deployer as a subprocess of itself, which is exactly this
    shape.

    `/.dockerenv` covers Docker; the env var is the honest override and is also
    how a non-Docker runtime says so.
    """
    if os.environ.get("HOME_STACK_ALLOW_CONTAINER_LOCAL") == "1":
        return False
    if os.environ.get("HOME_STACK_IN_CONTAINER") == "1":
        return True
    return Path("/.dockerenv").exists()


def _skip_ignore(skip: list[str]):
    """A `shutil.copytree` ignore that skips a name only if it *is* what it says.

    A backup that quietly leaves something out is the failure this whole file is
    written against, so "there is a directory called venv" is not enough to drop
    it. `venv/` and `.venv/` are skipped only when they contain `pyvenv.cfg` --
    which is what makes a directory a virtualenv (PEP 405), and which a
    household's own directory that happens to share the name will not have.

    `__pycache__` needs no such test: the name is reserved by the interpreter
    and its contents are bytecode for source that is being copied anyway.

    An entry in `skip` carrying a glob character is matched with fnmatch
    instead of compared. That is how a per-entry rule can name a *kind* of file
    -- `*.log` inside a plugin checkout -- without needing a second mechanism
    beside this one. A plain name still has to match exactly, so nothing that
    used to be copied stops being copied.
    """
    marked = {"venv", ".venv"}
    globs = [p for p in skip if any(c in p for c in "*?[")]
    exact = {p for p in skip if p not in globs}

    def ignore(directory, names):
        out = set()
        for name in names:
            if name in exact:
                if name in marked and not (
                        Path(directory) / name / "pyvenv.cfg").is_file():
                    continue      # same name, different thing -- copy it
                out.add(name)
            elif any(fnmatch.fnmatch(name, g) for g in globs):
                # Only ever a file: a glob that matched a directory would drop
                # a whole subtree on a name coincidence, which is the failure
                # the `pyvenv.cfg` confirmation above exists to prevent.
                if (Path(directory) / name).is_file():
                    out.add(name)
        return out

    return ignore


class Target:
    """A machine to deploy to. `local` when the address is this box."""

    def __init__(self, role: str, host_cfg: dict, dry_run: bool):
        self.role = role
        address = str(host_cfg.get("address") or "").strip()
        if not address:
            # KeyError here is not a DeployError, so main()'s handler never saw
            # it and an unfilled address reached the operator as a traceback.
            raise DeployError(
                f"hosts.{role} has no address. Fill it in config/home-stack.yml, "
                f"or on the admin page.")
        self.address = address
        self.user = host_cfg.get("user") or os.environ.get("USER", "root")
        self.dry_run = dry_run
        self.is_local = self._detect_local()

    def _detect_local(self) -> bool:
        if self.address in _LOOPBACK:
            if in_container():
                # "Local" means this machine, and inside a container it does
                # not. Three separate things go wrong quietly: a verify `curl`
                # to 127.0.0.1 reaches the container rather than the host --
                # measured, the portal answers 200 on the host and nothing at
                # all in here -- state directories are created inside the
                # container while Docker auto-creates root-owned ones on the
                # host, and a `pre:` script writes where nobody will look.
                #
                # Containers do start, because the Docker socket is mounted, so
                # the failure is not "nothing happened". It is worse: the work
                # lands on one machine and the checking on another.
                raise DeployError(
                    f"hosts.{self.role} is {self.address}, but this deployer is "
                    f"running inside a container -- so that address is the "
                    f"container, not the machine the services run on.\n"
                    f"    Deploy from a shell on the host, or give "
                    f"hosts.{self.role} the machine's real address so this "
                    f"connects over ssh with the keys it already mounts.\n"
                    f"    Set HOME_STACK_ALLOW_CONTAINER_LOCAL=1 only if this "
                    f"container really does share the host's network and paths."
                )
            return True
        try:
            own = subprocess.run(
                ["hostname", "-I"], capture_output=True, text=True, timeout=5
            ).stdout.split()
            return self.address in own
        except Exception:
            return False

    def __str__(self) -> str:
        return f"{self.role} ({'local' if self.is_local else self.address})"

    def run(self, command: str, cwd: str | None = None,
            env: dict | None = None, check: bool = True,
            capture: bool = False, timeout: int | None = None):
        """Run a shell command on the target."""
        if self.is_local:
            full = command
            if cwd:
                full = f"cd {shlex.quote(cwd)} && {command}"
            argv = ["bash", "-c", full]
            run_env = {**os.environ, **(env or {})}
            stdin_payload = None
        else:
            # Env goes through `env -` on the remote side rather than through
            # the ssh command line, so secrets never appear in the remote
            # process list or in this machine's shell history.
            # Environment goes over stdin, never in the argv. As an `export`
            # prefix it appeared in `ps auxww` on this machine for the length of
            # the command, and sshd exec'd the same string on the target, so it
            # was equally visible there -- which is exactly what this comment
            # used to claim was avoided.
            remote = command
            if cwd:
                remote = f"cd {shlex.quote(cwd)} && {remote}"
            script = "".join(
                f"export {k}={shlex.quote(v)}\n" for k, v in (env or {}).items()
            ) + remote
            argv = [
                "ssh", *deploy_identity(),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                f"{self.user}@{self.address}", "bash -s",
            ]
            run_env = dict(os.environ)
            stdin_payload = script

        if self.dry_run:
            shown = command if len(command) < 300 else command[:300] + " ..."
            out.sub(f"[dry-run] {self}: {shown}")
            return subprocess.CompletedProcess(argv, 0, "", "")

        try:
            return subprocess.run(
                argv, check=check, text=True, timeout=timeout,
                env=run_env,
                input=stdin_payload,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            if not detail and not self.is_local:
                # Without `capture`, stderr went to the terminal and is gone by
                # the time this is raised -- so a *connection* failure reads as
                # "the command failed", naming a mkdir that never ran. Ask ssh
                # once, quietly, purely to say why.
                #
                # This is what a changed host key looks like from here: the
                # deploy reported `mkdir -p ... failed` on a host it could not
                # open a session to at all.
                probe = subprocess.run(
                    ["ssh", *deploy_identity(),
                     "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                     f"{self.user}@{self.address}", "true"],
                    capture_output=True, text=True)
                if probe.returncode != 0:
                    why = (probe.stderr or "").strip().splitlines()
                    keep = [l for l in why if l.strip()
                            and not l.startswith("@")][:4]
                    raise DeployError(
                        f"cannot reach {self}, so nothing ran there:\n    "
                        + "\n    ".join(keep or ["ssh gave no reason"])
                    ) from exc
            raise DeployError(
                f"command failed on {self}: {command}"
                + (f"\n    {detail}" if detail else "")
                + self._disk_hint(detail)
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DeployError(f"command timed out on {self}: {command}") from exc

    def push_file(self, local_file: Path, remote_path: str) -> None:
        """Copy a single file, for seeding config the service ships an example of."""
        if self.dry_run:
            out.sub(f"[dry-run] copy {local_file.name} -> {self}:{remote_path}")
            return
        # A directory here is not a destination, it is a symptom: the daemon
        # creates one when a compose file bind-mounts a *file* that does not
        # exist yet. Both `cp` and `scp` would then copy *into* it, and the
        # source here is usually a temporary -- so the service gets a directory
        # where it expects its file, holding something called tmpct9ytros.json,
        # and every deploy adds another one. Observed on the first deploy of
        # the dashboard, which is how this comment is this specific.
        if self.run(f"test -d {shlex.quote(remote_path)}", check=False).returncode == 0:
            raise DeployError(
                f"{remote_path} is a directory, not a file. Docker creates one "
                f"when it bind-mounts a file that is not there yet; remove it "
                f"and deploy again.")
        if self.is_local:
            shutil.copy2(local_file, remote_path)
            return
        self._rsync(
            ["scp", "-q", *deploy_identity(), "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new",
             str(local_file), f"{self.user}@{self.address}:{remote_path}"],
            f"copying {local_file.name}")

    def _rsync(self, args: list[str], what: str) -> None:
        """Run an rsync/scp, and fail the way the rest of the deployer does.

        `subprocess.run(check=True)` here let CalledProcessError escape as a
        raw traceback -- so a disk that filled during a push printed a Python
        stack and a truncated argv, and never reached the message that says
        what actually went wrong.
        """
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            return
        detail = (result.stderr or "").strip()
        raise DeployError(
            f"{what} failed on {self}"
            + (f"\n    {detail.splitlines()[0]}" if detail else "")
            + self._owner_hint(detail)
            + self._disk_hint(detail))

    def _owner_hint(self, detail: str) -> str:
        """Say "somebody else owns this tree" when that is what happened.

        rsync reports it as `chgrp ... failed: Operation not permitted`, which
        reads like a permissions bug in the deployer rather than what it is: a
        directory written by a different account than the one deploying now.

        The way it happens here is a deploy from the admin page while that
        container still ran as root, followed by a deploy from a shell. Both
        are legitimate; the trees they leave are not interchangeable. The
        container runs as the deploying user now, so this is a hint about
        clearing up after the versions that did not.
        """
        if "Operation not permitted" not in detail and "Permission denied" not in detail:
            return ""
        # The deploy root only, and deliberately not the state tree. This hint
        # used to name both, and that advice broke a database: `paths.state`
        # holds directories a *service* owns under its own uid -- postgres runs
        # as 999, mosquitto as 1883 -- and chowning them to the deploying user
        # leaves a cluster that cannot read `global/pg_filenode.map` and
        # refuses every connection. The deploy root is the pushed tree and is
        # the deploying user's by definition; state is not.
        return ("\n    a tree here is owned by another account -- most likely "
                "root, from a deploy\n    that ran before the admin container "
                "dropped to your user. Give the pushed\n    tree back:\n"
                "      sudo chown -R \"$(id -un):$(id -gn)\" "
                f"{shlex.quote(str(Path(os.environ.get('HOME_STACK_DEPLOY_ROOT') or Path.home() / '.local/share/home-stack')))}\n"
                "    Not paths.state: the directories under it belong to the "
                "services that\n    run there, under their own uids, and "
                "taking those is how a database\n    stops being able to read "
                "its own files.")

    def _disk_hint(self, detail: str) -> str:
        """Say "the disk is full" when that is what happened.

        A build that fills the disk surfaces as whatever command happened to
        be running when the last byte went -- an rsync exiting 11, a docker
        build failing to write a layer -- and the reader is left with
        `rsync error: error in file IO (code 11)` and no idea it was space.
        Images here total around 30 GB and a rebuild needs headroom, so this is
        not an unusual way to fail.
        """
        # The explicit string only. rsync's code 11 is "error in file IO",
        # which is a full disk often enough to guess at and not often enough to
        # assert -- and a wrong diagnosis sends somebody to clean a disk that
        # was never the problem.
        if "No space left on device" not in detail:
            return ""
        # Reported, not asserted: by the time this runs, the failed build's
        # layers may already have been cleaned up, so the disk can look roomy
        # again. "It ran out" is what is known; "it is full now" is not.
        try:
            probe = self.run("df -h / | tail -1", check=False, capture=True)
            free = (probe.stdout or "").split()
            where = f" ({free[3]} of {free[1]} free now)" if len(free) > 3 else ""
        except Exception:                                   # noqa: BLE001
            where = ""
        return (f"\n    {self} ran out of disk{where}. Nothing was left "
                f"half-written -- the deploy stops before replacing a running "
                f"service -- but it did not finish.\n"
                f"    Reclaim what is not in use and run it again:\n"
                f"      docker image prune -af && docker builder prune -af")

    def pull(self, remote_path: str, local_path: Path,
             is_dir: bool = True, skip: "list[str] | None" = None) -> bool:
        """Fetch state *from* this target. The other direction from push().

        Backups need this and nothing else did, so it did not exist: the
        backup tool copied every path with shutil, which is correct only while
        every role points at this machine. Give `home-paperless` a real address
        -- which the config explicitly supports -- and the documents, the
        database and the camera recordings all read as absent, the backup
        verifies green because absent paths are skipped, and retention then
        deletes the last archive that had them.

        Returns False when the path is not there, which is an ordinary answer:
        a service can be enabled and not yet deployed.

        `skip` names directories not worth copying -- see `REGENERABLE` in
        backup.py. Local copies confirm what they skip by looking inside it;
        the remote path cannot, and matches on the name alone. That asymmetry
        is deliberate and is stated where it matters, in `REGENERABLE`.
        """
        if not self.is_local:
            probe = self.run(f"test -e {shlex.quote(remote_path)}",
                             check=False, capture=True)
            if probe.returncode != 0:
                return False
        elif not Path(remote_path).exists():
            return False

        local_path.parent.mkdir(parents=True, exist_ok=True)
        if self.is_local:
            src = Path(remote_path)
            if src.is_dir():
                ignore = _skip_ignore(skip) if skip else None
                shutil.copytree(src, local_path, dirs_exist_ok=True,
                                symlinks=True, ignore=ignore)
            else:
                shutil.copy2(src, local_path)
            return True

        # -a preserves ownership and times, which a restore needs; no --delete,
        # because the destination is a fresh directory inside the archive.
        args = ["rsync", "-a", "-e",
                "ssh " + " ".join(shlex.quote(a) for a in deploy_identity())
                + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new"]
        for name in (skip or []):
            args += ["--exclude", name]
        source = f"{self.user}@{self.address}:{remote_path}"
        if is_dir:
            source += "/"
            local_path.mkdir(parents=True, exist_ok=True)
        args += [source, str(local_path)]
        self._rsync(args, f"fetching {remote_path}")
        return True

    def push(self, local_dir: Path, remote_dir: str) -> None:
        """Ship a service directory. Excludes are what a container must never
        receive: local secrets, git metadata, and build residue."""
        if self.is_local and str(local_dir) == remote_dir:
            return
        excludes = list(TREE_EXCLUDES)
        args = ["rsync", "-a", "--delete"]
        for pattern in excludes:
            args += ["--exclude", pattern]
        args.append(f"{local_dir}/")
        if self.is_local:
            args.append(f"{remote_dir}/")
        else:
            args += ["-e", "ssh " + " ".join(shlex.quote(a) for a in deploy_identity())
                     + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new"]
            args.append(f"{self.user}@{self.address}:{remote_dir}/")

        if self.dry_run:
            # A staged build context lives under /tmp, so it has no path
            # relative to the repository to report.
            try:
                shown = local_dir.relative_to(ROOT)
            except ValueError:
                # A staged context lives under /tmp and a plugin unit lives
                # outside this repository; neither has a path relative to it.
                shown = (local_dir if local_dir.is_dir()
                         else "<staged build context>")
            out.sub(f"[dry-run] rsync {shown} -> {self}:{remote_dir}")
            return
        self.run(f"mkdir -p {shlex.quote(remote_dir)}")
        self._rsync(args, f"shipping {Path(local_dir).name}")


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def poll(target: Target, test_cmd: str, timeout: int, what: str,
         on_fail: str | None = None, cwd: str | None = None) -> None:
    """Poll until a command succeeds or the deadline passes.

    Written as a deadline rather than a sleep because the two failures it has
    to tell apart -- a service that is slow and a service that is broken --
    look identical for the first few seconds and completely different at 120.
    """
    if target.dry_run:
        out.sub(f"[dry-run] verify: {what}")
        return

    deadline = time.time() + timeout
    while True:
        result = target.run(test_cmd, cwd=cwd, check=False, capture=True)
        if result.returncode == 0:
            out.ok(what)
            return
        if time.time() >= deadline:
            out.fail(f"{what} - no answer within {timeout}s")
            if on_fail:
                out.fail(on_fail)
            raise DeployError(f"verification failed: {what}")
        time.sleep(3)


def verify(target: Target, checks: list, cfg: dict, env: dict,
           service_dir: str) -> None:
    for raw in checks:
        check = interpolate(raw, cfg)

        if "http" in check:
            url = check["http"]
            flags = "-sS -m 10"
            if check.get("insecure"):
                flags += " -k"
            expect = check.get("expect_json")
            if expect:
                # Assert the payload, not the status. `curl -sf` does not fail
                # on a 3xx, and this check once went green against a different
                # service's redirect on the same port while the real one
                # crash-looped.
                key, want = next(iter(expect.items()))
                # Match the value quoted or bare. Wrapping it in quotes
                # unconditionally meant a JSON boolean never matched: ntfy
                # answers {"healthy":true}, the check looked for "true", and a
                # perfectly healthy container failed its whole timeout.
                literal = "true" if want is True else "false" if want is False else str(want)
                cmd = (
                    f"curl {flags} {shlex.quote(url)} "
                    f"| grep -qE '\"{key}\"[[:space:]]*:[[:space:]]*\"?{literal}\"?'"
                )
            elif check.get("expect"):
                # The same idea for an endpoint that does not answer in JSON:
                # Homepage's healthcheck is the four bytes `up`, and "did it
                # answer" is satisfied by its own 404 page.
                cmd = (f"curl {flags} {shlex.quote(url)} "
                       f"| grep -qF {shlex.quote(str(check['expect']))}")
            elif check.get("expect_status"):
                # For a page whose body is generated, translated, or otherwise
                # not safe to grep, where the failure is the status itself.
                # `-f` is not enough: it treats every 4xx and 5xx alike and
                # passes on a 3xx, and "it redirected somewhere" has already
                # been mistaken here for "it works".
                want = int(check["expect_status"])
                cmd = (f"test \"$(curl {flags} -o /dev/null -w '%{{http_code}}' "
                       f"{shlex.quote(url)})\" = {want}")
            else:
                cmd = f"curl {flags} -f -o /dev/null {shlex.quote(url)}"
            if check.get("fallback_http"):
                cmd += f" || curl {flags} -f -o /dev/null {shlex.quote(check['fallback_http'])}"
            poll(target, cmd, check.get("timeout", 60), f"{url} answers",
                 check.get("fail_message"))

        elif "https_resolve" in check:
            # --resolve rather than a Host header, and never -k: the question
            # is whether a client trusting the right CA accepts what we serve
            # for that name.
            name = check["https_resolve"]
            port = check.get("port", 443)
            ca = check.get("ca")
            # Fail on a missing CA rather than silently dropping --cacert and
            # validating a local-CA certificate against the system trust store,
            # which can only ever fail -- and would fail for the wrong reason.
            ca_flag = ""
            if ca:
                probe = target.run(f"test -s {shlex.quote(ca)}", cwd=service_dir,
                                   check=False, capture=True)
                if probe.returncode == 0:
                    ca_flag = f"--cacert {shlex.quote(ca)}"
                elif not target.dry_run:
                    out.sub(f"no {ca} on the target; validating against the "
                            f"system trust store")
            # The path, and something the body has to contain. Both matter,
            # and this check had neither.
            #
            # Caddy answers an *empty 200* for a name whose TLS it can serve
            # but whose site block does not exist -- and the local CA
            # certificate carries `*.<domain>`, so it can serve TLS for every
            # name in the house. This check was `curl -f -o /dev/null` against
            # `/`, which passes on exactly that. The Caddyfile's site addresses
            # were literals from another household while this reported the
            # proxy healthy on every deploy.
            path = check.get("path", "/")
            url = f"https://{name}{path}"
            expect = check.get("expect_json")
            if expect:
                key, want = next(iter(expect.items()))
                literal = ("true" if want is True else "false" if want is False
                           else str(want))
                body = (f"| grep -qE '\"{key}\"[[:space:]]*:"
                        f"[[:space:]]*\"?{literal}\"?'")
            elif check.get("expect"):
                body = f"| grep -qF {shlex.quote(str(check['expect']))}"
            else:
                # No payload named: still better than -o /dev/null, because an
                # empty body is what the failure above looks like.
                body = "| grep -q ."
            cmd = (
                f"curl -sS -f {ca_flag} "
                f"--resolve {name}:{port}:127.0.0.1 {shlex.quote(url)} {body}"
            )
            # `fail_message` reaches poll here too. Two checks in the manifest
            # carry one -- and they are the ones whose failure is least
            # self-explanatory ("no site block matched it") -- and it was being
            # dropped on the floor.
            poll(target, cmd, check.get("timeout", 60),
                 f"{name} served with a trusted certificate",
                 check.get("fail_message"), cwd=service_dir)

        elif "chat_completion" in check:
            spec = check["chat_completion"]
            token = env.get(spec["bearer"], "")
            body = json.dumps({
                "channel": "voice",
                "chat_id": "deploy-smoke",
                "messages": [{"role": "user", "content": spec["prompt"]}],
            })
            cmd = (
                f"curl -sS -f -m {spec.get('timeout', 90)} "
                f"-X POST {shlex.quote(spec['url'])} "
                f"-H {shlex.quote('Authorization: Bearer ' + token)} "
                f"-H 'Content-Type: application/json' "
                f"-d {shlex.quote(body)} "
                f"| grep -qi {shlex.quote(spec['expect_contains'])}"
            )
            poll(target, cmd, spec.get("timeout", 90),
                 "the assistant answered a real turn")

        elif "container_env_absent" in check:
            spec = check["container_env_absent"]
            # `! docker exec ... | grep -q` reports the *pipeline's* status,
            # which is grep's. When docker itself fails -- container not
            # running, renamed, docker not on PATH -- grep sees no input, exits
            # 1, and `!` turns that into success. The one check protecting the
            # credential boundary could not fail in the case that mattered.
            # Capture the environment first, prove we got it, then search it.
            probe = target.run(
                f"docker exec {shlex.quote(spec['container'])} env",
                check=False, capture=True)
            if not target.dry_run:
                if probe.returncode != 0:
                    raise DeployError(
                        f"could not read {spec['container']}'s environment, so "
                        f"the credential boundary is unverified: "
                        f"{(probe.stderr or '').strip()[:200]}"
                    )
                if not (probe.stdout or "").strip():
                    raise DeployError(
                        f"{spec['container']} reported an empty environment; "
                        f"refusing to treat that as proof of anything"
                    )
                offending = [
                    line for line in probe.stdout.splitlines()
                    if re.search(spec["pattern"], line)
                ]
                if offending:
                    names = ", ".join(sorted(l.split("=")[0] for l in offending))
                    raise DeployError(
                        spec.get("fail_message",
                                 f"{spec['container']} holds environment it "
                                 f"must not have") + f": {names}"
                    )
            out.ok(f"{spec['container']} holds no credential it must not have")

        elif "mqtt_roundtrip" in check:
            spec = check["mqtt_roundtrip"]
            # Run the client inside the broker's own container. Shelling out to
            # mosquitto_pub on the target assumed mosquitto-clients was
            # installed there, which nothing checks and nothing installs -- the
            # poll spent its whole timeout collecting exit 127 while the broker
            # was up and healthy.
            container = spec.get("container", "mqtt-broker")
            cmd = (
                f"docker exec {shlex.quote(container)} "
                f"mosquitto_pub -h 127.0.0.1 -p 1883 "
                f"-t {shlex.quote(spec['topic'])} -m deploy-probe -q 1"
            )
            poll(target, cmd, spec.get("timeout", 30),
                 "broker accepts a publish")

        elif "container_healthy" in check:
            # For a service with no published port: the broker answers on
            # 8910 inside a bridge network and nothing on the host can reach
            # it, so every check this loop had was blind to whether it was
            # running at all. It was not -- `command:` under an ENTRYPOINT that
            # ends in `exec nanobot "$@"` became `nanobot python -m ...`, and
            # the container crash-looped through 73 restarts while the deploy
            # reported success, because the only thing verified was an agent's
            # port on the host.
            #
            # Docker's own answer, because the compose file already declares
            # the healthcheck and a second copy of it here would drift. A
            # crash-looping container is `restarting`, which is neither
            # `running` nor `healthy`, so this catches it either way.
            name = check["container_healthy"]
            fmt = ("{{.State.Status}} "
                   "{{if .State.Health}}{{.State.Health.Status}}{{else}}"
                   "none{{end}}")
            cmd = (f"test \"$(docker inspect --format {shlex.quote(fmt)} "
                   f"{shlex.quote(name)} 2>/dev/null)\" = \"running healthy\" "
                   f"|| test \"$(docker inspect --format {shlex.quote(fmt)} "
                   f"{shlex.quote(name)} 2>/dev/null)\" = \"running none\"")
            poll(target, cmd, check.get("timeout", 90),
                 f"{name} is running", check.get("fail_message"))

        elif "script" in check:
            # Unit-relative, like `test.mount.from` beside it: push() sends the
            # *contents* of the unit directory, so a repo-relative path here
            # resolves to <remote_dir>/services/<svc>/... and never exists.
            script = check["script"]
            args = " ".join(shlex.quote(str(a)) for a in check.get("args", []))
            cmd = f"python3 {shlex.quote(script)} {args}"
            result = target.run(cmd, cwd=service_dir, env=env, check=False,
                                capture=True, timeout=check.get("timeout", 180))
            if result.returncode != 0 and not target.dry_run:
                raise DeployError(
                    f"{script} failed\n    {(result.stderr or '').strip()}"
                )
            out.ok(f"{Path(script).name} passed")

        else:
            raise DeployError(
                f"unrecognised verify check {sorted(check)!r}. A check this "
                f"loop does not understand used to fall through and report "
                f"nothing, which made a typo in the manifest look like a pass."
            )


# --------------------------------------------------------------------------
# Deploying one service
# --------------------------------------------------------------------------

def build_vps_caddyfile(cfg: dict) -> str:
    """The vhost block for the VPS's own Caddy, with the household's domain.

    Generated rather than shipped: the file in git named `chat.home`, an
    internal name, which is the one thing it cannot be on a public host -- so
    everybody had to hand-edit it, and the instructions above it said so.

    Deliberately *not* installed. This writes the file; appending it to
    /etc/caddy/Caddyfile and reloading is left to a person, because that file
    belongs to the machine rather than to this stack and may already terminate
    TLS for things this deploy knows nothing about.
    """
    vps = (cfg.get("cloud") or {}).get("vps") or {}
    domain = str(vps.get("domain") or "").strip()
    email = str(vps.get("acme_email") or "").strip()
    port = vps.get("proxy_port", 8080)

    if not domain:
        return (
            "# No `cloud.vps.domain` is set, so there is no name to serve and\n"
            "# no certificate to get. The proxy answers on 127.0.0.1:"
            f"{port} and\n"
            "# whatever fronts it decides what it is called.\n"
            "#\n"
            "# Set cloud.vps.domain in config/home-stack.yml and deploy again.\n"
        )

    lines = [
        "# Generated by deploy/deploy.py from cloud.vps.domain.",
        "#",
        "# Append to the VPS's /etc/caddy/Caddyfile and `systemctl reload caddy`.",
        "# Not installed automatically: that file belongs to the machine, and it",
        "# may already terminate TLS for vhosts this stack knows nothing about.",
        "#",
        "# flush_interval -1 disables response buffering. The assistant answers",
        "# over SSE, and with buffering on a reply arrives as one lump when the",
        "# stream closes instead of word by word.",
    ]
    if email:
        lines += ["", "# Certificate expiry notices go here.", "{", f"\temail {email}", "}"]
    lines += [
        "",
        f"{domain} {{",
        "\tencode gzip",
        f"\treverse_proxy 127.0.0.1:{port} {{",
        "\t\tflush_interval -1",
        "\t}",
        "}",
        "",
    ]
    return "\n".join(lines)


# The four groups the Alfred app's Apps menu is drawn in. A tile names one to
# appear there; naming none leaves it on the wall only, which is what every
# tile did before this existed.
#
# Validated against this set rather than passed through, because the failure of
# a typo is invisible: `menu: cassa` would write a group the template never
# renders, the entry would simply not appear, and the plugin author would be
# looking at a correct-looking file. A deploy that stops and names the four is
# the cheaper answer.
#
# Which badge an entry gets is decided by its URL, not by its group -- an
# absolute `http://...` is a house-network link and is marked as one, a path is
# served by this app. That is the existing rule in chat.html and a plugin
# inherits it, so a plugin that gets itself proxied later changes its href and
# nothing else.
APP_MENU_GROUPS = ("panels", "professions", "casa", "settings")


def tile_menu(tile: dict, where: str) -> str:
    """The Apps-menu group a tile asked for, or "" for wall-only."""
    group = str(tile.get("menu") or "").strip().lower()
    if not group:
        return ""
    if group not in APP_MENU_GROUPS:
        raise DeployError(
            f"{where}: menu: {group!r} is not one of "
            f"{', '.join(APP_MENU_GROUPS)}")
    return group


def tile_icon(value) -> str:
    """An icon for a tile, or the one that means "somebody else's service".

    The dashboard draws whatever this returns as text, so an icon is an emoji
    -- not a name from an icon set. Plugins written against the gethomepage
    dashboard say `icon: mdi-light-switch`, which would render as the literal
    string; those are answered with the generic glyph rather than with their
    own name in six-point type. Anything entirely ASCII is such a name.
    """
    text = str(value or "").strip()
    return text if text and not text.isascii() else "\N{ELECTRIC PLUG}"


def build_opencode_config(member: str, token: str, port: int,
                          workspace: str = "") -> str:
    """`opencode.json` for one member's `opencode serve`.

    Deliberately a file of this stack's own, pointed at by OPENCODE_CONFIG,
    rather than an edit to ~/.config/opencode/opencode.json. That one belongs
    to whoever uses opencode at this keyboard -- it is their editor, and a
    deploy that rewrites it changes somebody's tools underneath them. opencode
    loads the global file first and this one over it, so their settings still
    apply and only the household's additions are ours.

    All this carries is the MCP block: where that member's bridge is, and the
    token that opens it. The agent's prompt is a separate file in the same
    directory, because a prompt inside JSON is a prompt nobody will edit. The
    provider credentials are in neither -- opencode reads those from its
    environment, so nothing here holds a household key.

    It also settles the permissions, and that is not a convenience. opencode's
    default for a directory outside the session's own is to **ask**, and there
    is nobody here to answer: the person is in a chat, that prompt has no UI
    there, and the turn waits for a reply that can never come. Measured -- a
    turn stalled at `asking permission=external_directory` and was still
    stalled five minutes later having printed nothing, which is exactly what
    "Alfred Programador is not doing anything" looks like from a chair.

    So the member's own checkout root is allowed outright and everything else
    keeps opencode's default. Narrower than it sounds: it is the one directory
    the broker checks that member's projects out into, it is already the
    session's working directory, and what guards it is the agent's tools map
    and the broker -- never a prompt nobody can see.
    """
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "alfred": {
                "type": "remote",
                # Loopback: the bridge publishes on 127.0.0.1 and this
                # member's opencode runs on the same host. What is behind the
                # port is one person's code and files.
                "url": f"http://127.0.0.1:{port}/mcp",
                "enabled": True,
                "headers": {
                    "X-Alfred-User": member,
                    "X-Alfred-Token": token,
                },
            }
        },
        # Globs, not prefixes: opencode matches these as patterns, and the root
        # itself is not covered by the one that ends in `/*`.
        #
        # `/tmp` is here because the work needs it and nobody can be asked for
        # it. Offering a local preview -- which this agent is told to do for
        # anything with a page -- means starting a server and putting its log
        # somewhere, and `nohup … > /tmp/x.log` is what any shell does. It got
        # as far as "opencode stopped to ask permission for /tmp/*" and stopped
        # there. A scratch directory every build on earth writes to is not the
        # thing worth a prompt that cannot be answered.
        #
        # This list stays short on purpose. What is *not* here still fails
        # loudly through HomeCore's `permission.asked` branch, which is the
        # point: a Programmer reaching for /etc or another member's checkout
        # should stop and say so, and it can only do that while the list of
        # directories it may take for granted is one somebody read.
        "permission": {
            # Git that *changes* something goes through the broker or not at
            # all. The prompt has said so since this shipped and the agent did
            # it anyway: asked to merge, it ran `git checkout master && git
            # merge` in a shell, fast-forwarded the trunk, and the broker's
            # `merge` -- which refuses a dirty tree, refuses a diverged trunk,
            # aborts on conflict and merges `--no-ff` -- then found nothing to
            # do and reported success. Every one of those guards was bypassed
            # by a tool the agent already had.
            #
            # Reading is left alone: `git log`, `git diff`, `git show` and
            # `git status` are how it answers "what is on this branch", and
            # nothing about them can lose work. Only the verbs that move a ref,
            # write a commit or talk to a remote are refused, and the refusal
            # names the tool to use instead -- a denial the agent cannot act on
            # is one it will work around.
            # The catch-all goes **first**. opencode resolves these by walking
            # the entries and letting the last match win, so with `*` written
            # last it decided everything: `git checkout master` was evaluated
            # against this block and came back `action.pattern=* action=allow`,
            # and the agent merged the trunk by hand anyway. Measured, after
            # writing it the other way round and watching it not fire.
            # Every pattern is wrapped in `*`, and that is the difference
            # between a rule and a suggestion. These match the whole command
            # line, so a pattern anchored at `git ` covers only the spelling
            # that happens to start with it: `cd proyecto && git checkout
            # master`, `git -C /srv/proyecto merge alfred/x`, `env git push`,
            # `/usr/bin/git commit -m x` and `sh -c 'git reset --hard'` all
            # walk past it into the catch-all. The measured bypass -- `git
            # checkout master && git merge` -- was caught only because it
            # happened to be typed with git first, and an agent told to work
            # around a refusal will not type it that way twice.
            #
            # The cost is that a `grep 'git push' README.md` is refused too.
            # That is the right side to be wrong on: a denial the agent can
            # rephrase, against a guard that would otherwise be spelling-deep.
            "bash": {
                "*": "allow",
                # Writes a commit.
                "*git commit*": "deny",
                "*git revert*": "deny",
                "*git am*": "deny",
                # Moves a ref, or the work under it.
                "*git merge*": "deny",
                "*git rebase*": "deny",
                "*git cherry-pick*": "deny",
                "*git checkout*": "deny",
                "*git switch*": "deny",
                "*git branch*": "deny",
                "*git reset*": "deny",
                "*git tag*": "deny",
                "*git update-ref*": "deny",
                "*git symbolic-ref*": "deny",
                "*git bisect*": "deny",
                "*git filter-branch*": "deny",
                "*git worktree*": "deny",
                "*git submodule*": "deny",
                # Loses uncommitted work, or hides it. `stash` is here for the
                # second reason: stash, merge, pop is how a dirty tree gets
                # past the broker's refusal to merge one.
                "*git restore*": "deny",
                "*git clean*": "deny",
                "*git stash*": "deny",
                "*git rm *": "deny",
                "*git mv *": "deny",
                # Talks to a remote, or decides which one.
                "*git push*": "deny",
                "*git pull*": "deny",
                "*git fetch*": "deny",
                "*git remote*": "deny",
                # The verb patterns above match a literal `git <verb>`, so a
                # word between the two slips past every one of them. Measured,
                # after the patterns above were widened and believed to cover
                # it: `git -C . merge master` ran, and answered "Already up to
                # date." These forms are how you point git at another tree, and
                # the agent works in the checkout it was given, so refusing the
                # whole shape costs it nothing it needs.
                "*git -C*": "deny",
                "*git --git-dir*": "deny",
                "*git --work-tree*": "deny",
                "*git --exec-path*": "deny",
                "*GIT_DIR=*": "deny",
                "*GIT_WORK_TREE=*": "deny",
                # Last match wins, so the read-only spellings come back after
                # the blanket refusals above. `git branch` and `git tag` with
                # no arguments *list*; refusing them refuses the plainest
                # answer to "what is on this checkout", which the paragraph
                # above promises is the agent's own. Exact strings, not globs:
                # an argument that could mutate must not slip in behind one.
                "git branch": "allow",
                "git branch -a": "allow",
                "git branch -v": "allow",
                "git branch -vv": "allow",
                "git branch --all": "allow",
                "git branch --list": "allow",
                "git branch --show-current": "allow",
                "git tag": "allow",
                "git tag -l": "allow",
                "git tag --list": "allow",
                "git remote -v": "allow",
            },
            "external_directory": ({
                f"{workspace.rstrip('/')}/{member}": "allow",
                f"{workspace.rstrip('/')}/{member}/*": "allow",
            } if workspace else {}) | {
                "/tmp": "allow",
                "/tmp/*": "allow",
            },
        },
    }, indent=2) + "\n"


def build_portal_dashboard(cfg: dict, plugins: list | None = None) -> str:
    """The portal's own dashboard, as JSON the portal reads at runtime.

    Three groups, and they answer three different questions:

    * **basic** -- what somebody opens on purpose, most weeks.
    * **advanced** -- real services, but plumbing or occasional: the broker's
      dashboard when a sensor goes quiet, the admin page when something needs
      changing.
    * **extensions** -- everything this repository does not deploy. A
      household's own services arrive here, whether declared by a plugin or
      typed into the admin page as a custom service, and they are marked so
      that a tile which is nobody's responsibility here looks like one.

    The tier is declared in `deploy/manifest.yml` beside the service, so adding
    a service is one line and the dashboard follows -- rather than a list here
    that somebody has to remember. `tier: none` means no tile at all: the
    assistants answer through the portal and the proxies are plumbing.

    `title` and `description` here are English, and are a *fallback*. The
    portal looks each tile up by `name` in the shared catalogue first, so a
    house running in French gets French tiles without the deployer knowing
    which locale anybody chose. An extension has no catalogue entry and keeps
    whatever the household typed, which is the right answer for a name the
    household invented.

    Two kinds of link, and the difference decides `lan_only`.

    A service the portal *serves* -- declared `portal_path:` in the manifest --
    is linked by that path. Those reach the house from outside, because the VPS
    forwards the portal and the portal forwards them, so whether one is house
    only is the household's `house_only:` answer and nothing else.

    Everything else is linked by address and port, and is house only whatever
    anybody says. The VPS runs one proxy, it forwards to `home-core` and
    nothing else, and these addresses are private to the house network. A
    dashboard offering those from a phone on mobile data would be offering a
    row of connection timeouts, so the portal hides them when `_at_home()` is
    false rather than drawing links it knows are dead.
    """
    manifest = load_yaml(MANIFEST)
    manifest["_plugins"] = plugins or []
    services = cfg.get("services") or {}

    def enabled(name):
        return (services.get(name) or {}).get("enabled", True)

    lan_only_keys = set(house_only_apps(cfg).split(",")) - {""}
    groups = {"basic": [], "advanced": [], "extensions": []}

    for name, spec in all_services(manifest, cfg).items():
        tier = spec.get("tier", "none")
        if tier not in ("basic", "advanced") or not enabled(name):
            continue
        conf = services.get(name) or {}
        port_key = next((k for k in ("port", "web_port", "dashboard_port")
                         if isinstance(conf.get(k), int)), None)
        if not port_key:
            continue
        path = spec.get("portal_path")
        if path:
            url, lan_only = path, HOUSE_ONLY_APPS.get(name) in lan_only_keys
        else:
            address = browsable_address(cfg, spec.get("role", "hub"))
            # `tile_path:` is the path on that service's *own* port, for the
            # ones whose root is not a page. faster-whisper and home-voice are
            # FastAPI apps: `/` is a 404 and `/docs` is what a person can read.
            # A tile that 404s is worse than no tile, because it reads as the
            # service being down -- which is exactly the question somebody
            # opens it to answer.
            url = f"http://{address}:{conf[port_key]}{spec.get('tile_path', '')}"
            lan_only = True
        groups[tier].append({
            "name": name,
            "title": spec.get("title") or name,
            "description": (spec.get("description") or "").strip().split(".")[0],
            "icon": spec.get("icon") or "\N{LINK SYMBOL}",
            "url": url,
            "lan_only": lan_only,
            # Not whether a child *may* open it -- the admin page has its own
            # password for that -- but whether it belongs on their wall.
            "adults": bool(spec.get("adults")),
            "source": "system",
        })

    # A household's own services. Two ways in, and both are theirs rather than
    # this repository's, which is the whole point of the group.
    #
    # `lan_only` is the household's to declare here, and defaults to false --
    # the opposite of the system tiles above. This stack knows where it put its
    # own services and can tell you they are not published; it knows nothing
    # about a URL somebody typed, and guessing "house only" about a link to
    # something on the public internet would hide it for no reason.
    for plugin in plugins or []:
        for tile in (plugin["doc"].get("tiles") or []):
            # Refused, not skipped. A plugin that declares a tile and gets a
            # blank wall has no way to find out why, and a `tiles:` block is
            # the plugin author saying they want a square.
            for field in ("name", "href"):
                if not tile.get(field):
                    raise DeployError(
                        f"plugin {plugin['name']}: a tile has no {field}")
            groups["extensions"].append({
                "name": tile["name"], "title": tile["name"],
                "description": tile.get("description", ""),
                "icon": tile_icon(tile.get("icon")),
                # Against the config, like everything else a plugin writes:
                # `{hosts.hub.address}` in an href is the point of the block.
                "url": interpolate(tile["href"], cfg),
                "lan_only": bool(tile.get("lan_only")),
                "adults": bool(tile.get("adults")),
                "menu": tile_menu(tile, f"plugin {plugin['name']}"),
                "source": f"plugin:{plugin['name']}"})
    for custom in (cfg.get("custom_services") or []):
        if not custom.get("name") or not custom.get("url"):
            continue
        groups["extensions"].append({
            "name": custom["name"], "title": custom.get("title") or custom["name"],
            "description": custom.get("description", ""),
            "icon": tile_icon(custom.get("icon")),
            "url": custom["url"], "lan_only": bool(custom.get("lan_only")),
            "adults": bool(custom.get("adults")),
            "menu": tile_menu(custom, f"custom_services {custom['name']}"),
            "source": "custom"})

    for rows in groups.values():
        rows.sort(key=lambda r: r["title"].lower())
    return json.dumps({"groups": groups, "generated_by": "deploy/deploy.py"},
                      ensure_ascii=False, indent=2) + "\n"


def build_family_directory(cfg: dict) -> str:
    """The household, as JSON the portal reads at request time.

    The family directory is the assistant's answer to "who lives here" -- names,
    birthdays, who is whose parent -- and it was filled in by hand, in a second
    place, against a page that already knows all of it. Two records of one
    household drift, and the one that drifts is the one nobody is looking at:
    a member renamed on the admin page kept the old name in every answer the
    assistant gave.

    So the admin page is the source and this is the copy, regenerated on every
    deploy. Keyed on the member's *folder*, because that is what the rest of
    the portal keys a person on -- the tasks nicknames, the share, the finance
    dashboard -- and a directory keyed on anything else is a fifth thing to
    join.

    What is *not* here is as deliberate: the facts and notes a household or an
    assistant adds ("Colegio", "takes a pill at 7") have no field on the admin
    page and are not invented here. They live in the directory's own tables
    and this file does not mention them, so regenerating it cannot remove them.
    """
    people = []
    by_id = {str(m.get("id") or "").strip(): m for m in (cfg.get("members") or [])}
    timezone = (cfg.get("site") or {}).get("timezone", "UTC")
    for mid in member_ids(cfg):
        member = by_id.get(mid)
        if member is None:
            continue
        # Relationships resolve to the other person's folder, the same key this
        # file is indexed by. Stored against member ids on the admin page,
        # because that is what is stable; joined here, once, so nothing
        # downstream has to know both.
        relations = {}
        for other_id, term in (member.get("relationships") or {}).items():
            other = by_id.get(str(other_id).strip())
            if other is not None and str(term or "").strip():
                relations[share_folder(other)] = str(term).strip()
        people.append({
            "person": share_folder(member),
            "member": mid,
            "display_name": str(member.get("display_name") or mid),
            "birthdate": str(member.get("birthdate") or ""),
            "timezone": timezone,
            "locale": str(member.get("locale") or ""),
            "admin": bool(member.get("admin")),
            "active": bool(member.get("active", True)),
            # One line each, the way the profile page takes them.
            "interests": [h.strip().lstrip("-").strip()
                          for h in str(member.get("hobbies") or "").splitlines()
                          if h.strip().lstrip("-").strip()],
            "notes": str(member.get("notes") or "").strip(),
            "relations": relations,
        })
    return json.dumps({"people": people, "generated_by": "deploy/deploy.py"},
                      ensure_ascii=False, indent=2) + "\n"


def build_member_profile(cfg: dict, member: dict) -> str:
    """One member's USER.md, from what the admin page knows about them.

    Seeds the assistant's workspace on that member's first boot and never
    overwrites it after -- the assistant's own copy evolves in conversation,
    and a deploy must not reset what a person has taught it.
    """
    by_id = {m["id"]: m for m in (cfg.get("members") or [])}
    lines = ["# User Profile", "", "## Basic Information", ""]
    lines.append(f"- **Name**: {member.get('display_name', member['id'])}")
    if member.get("birthdate"):
        lines.append(f"- **Birthdate**: {member['birthdate']}")
    lines.append(f"- **Timezone**: {(cfg.get('site') or {}).get('timezone', 'UTC')}")
    lines.append(f"- **Language**: {member.get('locale', 'en')}")
    lines += ["", "## Home Context", ""]
    if member.get("admin"):
        lines.append("- **Role in household**: administrator of the house")
    relations = member.get("relationships") or {}
    named = [(by_id[other]["display_name"], rel)
             for other, rel in relations.items() if other in by_id and rel]
    if named:
        lines += ["", "## Family", "",
                  "| Name | Relationship |", "|------|--------------|"]
        for name, rel in named:
            lines.append(f"| {name} | {rel} |")
    if member.get("hobbies"):
        lines += ["", "## Topics of Interest", ""]
        for hobby in str(member["hobbies"]).splitlines():
            hobby = hobby.strip().lstrip("-").strip()
            if hobby:
                lines.append(f"- {hobby}")
    if member.get("notes"):
        lines += ["", "## Special Instructions", "", str(member["notes"]).strip()]
    lines.append("")
    return "\n".join(lines)


def allow_env_keys(config_json: str, keys: list[str]) -> str:
    """Add keys to `tools.exec.allowedEnvKeys` in a nanobot config.

    A skill's generated code reads its service's address out of the
    environment, and the exec tool only passes through what this list names --
    so without it a plugin's skill runs, finds nothing, and reports the service
    as down. Additive only: the list is a security boundary (it is what keeps
    the room-facing assistant from holding per-member credentials) and a
    contribution may extend it, never rewrite it.
    """
    if not keys:
        return config_json
    doc = json.loads(config_json)
    exec_block = doc.setdefault("tools", {}).setdefault("exec", {})
    allowed = exec_block.setdefault("allowedEnvKeys", [])
    for key in keys:
        if key not in allowed:
            allowed.append(key)
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# There is no local image service any more. `services/z-image` drew on the
# house's own GPU behind a `z-image:` model prefix that this function
# recognised and turned into an endpoint; it was removed on 2026-09-08 and the
# prefix went with it, because a prefix naming a container nothing starts
# exports an address that refuses every connection.
#
# `docs/local-generation.md` keeps the measurements and says what a future one
# would have to re-add: **two halves in two files**, a catalogue entry in
# `admin/models.py` and the prefix here. They were two literals that had to
# agree, and `admin/test_models.py` existed to catch them drifting apart.


def image_model_env(cfg: dict) -> dict:
    """`IMAGE_MODEL` / `IMAGE_MODEL_HIGH` for the assistants, from the config.

    The theme skill reads `TOGETHER_IMAGE_MODEL` and nothing ever exported it,
    so every backdrop this house has generated came from the literal default in
    that script -- a setting listed in `allowedEnvKeys`, documented in
    AGENTS.md, and decorative.

    Both names carry the provider prefix the picker stores (`together:...`) so
    a skill can tell which endpoint to call; `TOGETHER_IMAGE_MODEL` keeps the
    bare id, because that is the shape the existing script already passes
    straight to Together.

    The Together test is against `MODEL_PROVIDERS` rather than the literal
    "together". The prefix a picker stores and the provider `split_model`
    returns are two different strings -- `together:` maps to `together_ai` --
    and comparing against the wrong one made this function do exactly what it
    was written to stop: export the two neutral names, never the Together one
    the theme skill actually reads, and leave the literal in that script the
    real setting.
    """
    together = MODEL_PROVIDERS["together"]
    models = ((cfg.get("assistant") or {}).get("models") or {})
    env = {}
    # Per role, not once: the two roles can sit on different providers, and
    # nothing says they must agree. A single endpoint variable would send the
    # good one to the cheap model or the reverse, and either way the setting on
    # the admin page would be a lie.
    # `IMAGE_API_URL` / `_HIGH` are deliberately not exported here any more.
    # They were synthesised for the local container; a household pointing a
    # role at some other endpoint sets them in the env file, and the skill
    # reads them from there.
    for role, prefixed, bare in (
            ("image_normal", "IMAGE_MODEL", "IMAGE_API_MODEL"),
            ("image_high", "IMAGE_MODEL_HIGH", "IMAGE_API_MODEL_HIGH")):
        value = models.get(role, "")
        if not value:
            continue
        env[prefixed] = value
        name, provider = split_model(value)
        # The bare id, for whoever posts it to an API. Exported for every
        # provider rather than only Together, because the skill that draws now
        # has more than one endpoint it might be pointed at and the Together
        # name says the wrong thing about the others.
        env[bare] = name
        if provider == together:
            env["TOGETHER_IMAGE_MODEL" if role == "image_normal"
                else "TOGETHER_IMAGE_MODEL_HIGH"] = name
    return env


# The token a shipped prompt uses to name the household's language. Doubled
# braces so it cannot collide with the single-brace `{...}` interpolation the
# manifest uses, and so it is obvious in prose that it is a placeholder.
HOUSEHOLD_LANGUAGE_TOKEN = "{{HOUSEHOLD_LANGUAGE}}"


def household_language(cfg: dict) -> str:
    """The household's language, named the way its own speakers name it.

    `es` -> `Español`, from that locale's own `_meta.name`. The endonym rather
    than the code because this goes into a prompt: "answer in Español" is
    unambiguous to a model in a way that "answer in es" is not, and a small
    model reading an English system prompt and English instructions will
    otherwise answer in English -- which is what qwen3.5:9b did on the first
    three notifications it served.
    """
    locale = str(((cfg.get("locale") or {}).get("default") or "en")).strip()
    catalogue = ROOT / "i18n" / f"{locale}.json"
    try:
        return json.loads(catalogue.read_text(encoding="utf-8"))["_meta.name"]
    except (OSError, ValueError, KeyError):
        # A locale with no catalogue is still a language; naming it by code is
        # worse than nothing only if it is empty, and it is not.
        return locale


def apply_household_language(text: str, cfg: dict) -> str:
    """Fill in HOUSEHOLD_LANGUAGE_TOKEN. A no-op for text that has none."""
    if HOUSEHOLD_LANGUAGE_TOKEN not in text:
        return text
    return text.replace(HOUSEHOLD_LANGUAGE_TOKEN, household_language(cfg))


# The engines a harness may drive: each is reached through the variables the
# assistants already carry for it, so the harness never names an address of its
# own. `llamacpp:` is the llama.cpp router, which cloud.openai_compatible points
# at. A hosted provider is not here: the harness runs unattended, and OpenCode
# in particular must never be reached that way (see CLAUDE.md).
HARNESS_ENGINES = {
    "ollama": ("${OLLAMA_URL}/v1", "${OLLAMA_API_KEY}", True),
    "llamacpp": ("${OPENAI_COMPATIBLE_URL}/v1", "${OPENAI_COMPATIBLE_API_KEY}", False),
    "openai-compatible": ("${OPENAI_COMPATIBLE_URL}/v1", "${OPENAI_COMPATIBLE_API_KEY}", False),
    "freetoken": ("${FREETOKEN_URL}/v1", "${FREETOKEN_API_KEY}", False),
}


def harness_settings(harness: dict, cfg: dict) -> dict:
    """`assistant.harness` as nanobot's `agents.defaults.harness`.

    `enabled` sends a member's own background tasks to pi, which runs the models
    picked for the sub-agent and the powerful sub-agent. `allow_go` lets those
    be OpenCode Go models -- the household's explicit exception to CLAUDE.md's
    rule that Go is for a person at the keyboard. `model` is an optional
    override, written like `assistant.models` (`ollama:gemma4:e4b`), and must
    then be a local engine.
    """
    enabled = bool(harness.get("enabled"))
    out = {"enabled": enabled, "engine": "pi", "allowGo": bool(harness.get("allow_go"))}
    model = str(harness.get("model") or "").strip()
    if model:
        prefix, sep, name = model.partition(":")
        if not sep or prefix not in HARNESS_ENGINES or not name:
            raise DeployError(
                f"assistant.harness.model is {model!r}: an override has to be a local model -- "
                f"{', '.join(p + ':<model>' for p in HARNESS_ENGINES)}. Leave it empty and pi "
                "runs the sub-agent models picked on the Models page.")
        base, key, reasoning = HARNESS_ENGINES[prefix]
        out.update(baseUrl=base, model=name, apiKey=key, reasoning=reasoning)
        context = ollama_context(cfg, "ollama")
        if prefix == "ollama" and context:
            out["contextWindow"] = int(context)
    for k in ("timeout_s", "max_nudges", "keep_workdirs"):
        if harness.get(k) is not None:
            out["".join(w if i == 0 else w.capitalize() for i, w in enumerate(k.split("_")))] = int(harness[k])
    return out


def apply_model_choices(config_json: str, cfg: dict) -> str:
    """Write `assistant.models` into a nanobot config, returning the new text.

    The site config is where a model is chosen, and this is what makes that
    true: without it the choice lives in the assistant's own config.json, which
    the admin page does not manage and a person editing it would have to know
    exists in two places (the per-member one and the house one).

    Only the keys the site config names are touched. Anything else in the file
    — every `_comment`, the provider blocks, the whole bake-off record — is
    left exactly as it was, because that file is also documentation.

    `ollama:<name>` selects the local provider for that role; a bare name is an
    OpenCode Zen model. The provider has to move with the model or the request
    goes to the wrong endpoint with the wrong key.
    """
    models = ((cfg.get("assistant") or {}).get("models") or {})
    effort = ((cfg.get("assistant") or {}).get("reasoning_effort") or {})
    # Both, not just the models: a household that only names thinking levels
    # still has a setting that has to reach the container, and returning here
    # on an empty `models` dropped it on the floor.
    if (not models and not effort and not (cfg.get("assistant") or {}).get("routing")
            and not (cfg.get("assistant") or {}).get("harness")):
        return config_json

    doc = json.loads(config_json)
    defaults = doc.setdefault("agents", {}).setdefault("defaults", {})
    # A provider block for every local instance the file does not already
    # carry. `ollama` and `ollama_vision` are in the shipped files, read from
    # the environment; any other instance is added here with its address
    # outright -- it holds no credential, and a new ${VAR} per instance would
    # be a compose change per instance.
    for inst in ollama_instances.serving(cfg):
        provider = ollama_instances.provider_of(inst["id"])
        if provider in ("ollama", "ollama_vision") or provider in (doc.get("providers") or {}):
            continue
        doc.setdefault("providers", {})[provider] = {
            "apiKey": "ollama", "apiBase": f"{ollama_instance_url(cfg, inst)}/v1"}
    profiles = defaults.setdefault("modelProfiles", {})
    providers = defaults.setdefault("providerProfiles", {})

    split = split_model

    # The roles that are not profiles: they name a model field directly.
    #
    # `subagent` and `subagent_powerful` are here because they were not, and
    # the gap was invisible until a household moved providers: everything in
    # `assistant.models` went to together.ai and the sub-agents kept the
    # shipped OpenCode Go default, because the site config had no key for
    # them. Every background task -- a cron reminder, the morning summary,
    # memory consolidation -- went on calling the provider the household had
    # just left, and the setting that looked like it covered the assistant
    # covered five of its eight routes.
    # A sub-agent follows the model it is a sub-agent *of*, unless the
    # household says otherwise. `subagent` inherits `everyday`,
    # `subagent_powerful` inherits `powerful`.
    #
    # These two exist because they did not, and the gap was invisible: a
    # household moved to together.ai, every role in `assistant.models` went
    # with them, and the sub-agents kept the shipped default -- so every
    # background task, every cron reminder, the morning summary and memory
    # consolidation went on calling the provider they had just left. Adding
    # the keys made that *settable*. Inheriting makes it right by default,
    # which is the version that survives the next move: a household that
    # changes `everyday` and does not think about sub-agents no longer has to.
    #
    # An explicit value still wins, and blank is how you ask to inherit -- so
    # "the long or complex kind, on a bigger model" stays one line of config
    # and is not the only way to be correct.
    for key, model_field, provider_field in (
            ("everyday", "model", "provider"),
            ("powerful", "modelPowerful", "providerPowerful"),
            ("subagent", "subagentModel", "subagentProvider"),
            ("planner", "planModel", "planProvider"),
            ("plan_steps", "planStepModel", "planStepProvider"),
            # The heartbeat's decision turn. Unset inherits the agent's
            # model, which is what it did before it had a key -- so a
            # household that never names one is unchanged.
            ("heartbeat", "heartbeatModel", "heartbeatProvider"),
            ("classifier", "classifierModel", "classifierProvider")):
        value = str(models.get(key) or "").strip()
        if not value and key in SUBAGENT_INHERITS:
            value = str(models.get(SUBAGENT_INHERITS[key]) or "").strip()
        if value:
            name, provider = split(value)
            defaults[model_field] = name
            defaults[provider_field] = provider
    # The retired powerful sub-agent: taken out of the file too, or the shipped
    # config.json's own value would go on answering complex background tasks.
    defaults.pop("subagentModelPowerful", None)
    defaults.pop("subagentProviderPowerful", None)

    if "vision" in models:
        name, provider = split(models["vision"])
        defaults["visionModel"] = name
        defaults["visionProvider"] = provider

    # Thinking, per role. `assistant.reasoning_effort` is a map of role -> the
    # value nanobot should send, and it exists because the setting nanobot had
    # was per *provider*: turning thinking off for notification triage would
    # have turned it off for the professions too.
    #
    # The case it was added for: a reasoning model asked to triage six
    # notifications spends thousands of tokens thinking, hits the cap and
    # returns EMPTY content with `finish_reason: "length"`. The turn does not
    # fail -- it silently says nothing, which reaches the family as an
    # assistant that stopped noticing things. Measured on Qwen3.6-35B-A3B:
    # unset and "low" both produced an empty answer, "none" produced clean
    # JSON. "low" is worse than unset, not better.
    #
    # Three fields carry a role. `everyday` reaches the default turn -- ordinary
    # chat and the sub-agents, which are neither a Profession nor `powerful`.
    # It had nowhere to go until loop.py grew `reasoning_effort_default`; before
    # that an entry here was silently dropped, which is why the note below still
    # warns about roles that reach no turn.
    if effort.get("powerful"):
        defaults["reasoningEffortPowerful"] = str(effort["powerful"])
    if effort.get("everyday"):
        defaults["reasoningEffortDefault"] = str(effort["everyday"])
    # The heartbeat has its own key because it has its own code path: it does
    # not go through the agent loop, so neither of the two above reaches it.
    if effort.get("heartbeat"):
        defaults["heartbeatReasoningEffort"] = str(effort["heartbeat"])
    if effort.get("classifier"):
        defaults["classifierReasoningEffort"] = str(effort["classifier"])
    # `assistant.routing` is the turn classifier's rules -- see
    # services/nanobot/nanobot/agent/classify.py and docs/routing.md. Written
    # whole, in the camelCase the config file speaks, so a key the site config
    # drops falls back to nanobot's own default rather than to a stale value.
    routing = (cfg.get("assistant") or {}).get("routing")
    if isinstance(routing, dict):
        defaults["routing"] = {
            "".join(w if i == 0 else w.capitalize() for i, w in enumerate(k.split("_"))): v
            for k, v in routing.items()
        }
    # `assistant.harness`: background tasks on pi (services/nanobot/nanobot/
    # harness/pi_runner.py) instead of nanobot's own loop. Written whole every
    # deploy, so switching it off here switches it off in every container.
    harness = (cfg.get("assistant") or {}).get("harness")
    if isinstance(harness, dict):
        defaults["harness"] = harness_settings(harness, cfg)

    # How many tokens of conversation history a turn may carry. Unset, nanobot
    # budgets from contextWindowTokens (65,536) minus the output reserve, which
    # is ~57k -- roughly double what this engine can cache.
    #
    # Measured 2026-09-10 on qwen3.6-35b-a3b: a prompt under ~28k tokens is
    # retained and a repeat turn costs ~2-3s; above ~30k nothing is retained
    # and every turn pays full prefill (~21s at 29k). It is a cliff, not a
    # slope, so the budget is worth setting deliberately rather than leaving to
    # a default that was chosen for a different constraint.
    #
    # The shared prompt head is ~9.1k, so a history budget of 16k lands a turn
    # near 25k -- inside the cliff with room for the per-turn tail.
    ctx_limit = ((cfg.get("assistant") or {}).get("context_block_limit"))
    if ctx_limit:
        defaults["contextBlockLimit"] = int(ctx_limit)

    # Only roles the assistant actually routes. `loop.py` applies a profile's
    # effort when that profile is in its model roster, so an entry for
    # `everyday`, a sub-agent or a misspelt profession reaches no turn -- and
    # writing it anyway would put a key in the rendered config that describes
    # nothing, which is the same "setting that never arrives" the warning
    # beside it exists to prevent. The model loop above already drops rather
    # than writes for exactly this reason; this now matches it.
    per_profile = {role: str(value) for role, value in effort.items()
                   if role != "powerful" and value and role in profiles}
    if per_profile:
        existing = dict(defaults.get("reasoningEffortProfiles") or {})
        existing.update(per_profile)
        defaults["reasoningEffortProfiles"] = existing

    # No provider key of its own, and that is the design rather than an
    # omission: the swap happens inside whichever provider instance the failing
    # route already built, so a fallback naming a second provider could not be
    # honoured. check_fallback_model refuses one before we get here.
    # A chain, not a name: the household's choice first, then the internal list.
    # One fallback shares its fate with the model it rescues -- see
    # OUTAGE_FALLBACKS for the outage that proved it.
    #
    # Anything a role already uses is dropped from the *internal* list. During
    # an outage the whole house swings onto these at once, so pointing two
    # roles at one name concentrates exactly the load this is meant to spread.
    # The household's own entries are kept even when a role names them:
    # `check_fallback_model` refuses only the one every role names, because a
    # local `everyday` model is still the offline rescue for every hosted role
    # -- and dropping it here would turn that accepted setting into no
    # fallback at all, silently.
    #
    # The internal list extends the household's choice; it does not stand in for
    # one nobody made. `check_fallback_model` returns early on an empty value
    # and its own error text offers "leave it empty for no fallback", so a house
    # that cleared the setting has said something -- and the list is only known
    # to be reachable *because* that check proved the configured fallback is a
    # hosted model on this gateway. Appending these to a house whose roles run
    # on `ollama:` would put three OpenCode Zen names in front of a local base
    # URL: three guaranteed failures on the end of every exhausted ladder, which
    # is the exact shape that check exists to refuse.
    if "fallback" in models:
        _raw = models["fallback"]
        _wanted = [str(v).strip()
                   for v in (_raw if isinstance(_raw, list) else [_raw])
                   if str(v or "").strip()]
        # The Zen extras belong to the *first* entry's provider, for the reason
        # the paragraph above gives: they are three bare names only that
        # gateway routes. A chain whose first entry is local gets them only if
        # some later entry is on Zen, and then only after it.
        # `titles` and `documents` are read by home-core and Paperless, never
        # by this assistant, so they are no role's rescue to protect.
        in_use = {split_model(str(v))[0] for k, v in models.items()
                  if k not in ("fallback", "titles", "documents")
                  and str(v or "").strip()}
        # The internal list is a property of *OpenCode Zen* -- three bare names
        # that gateway routes. Appended to a fallback on any other provider
        # they are three guaranteed 404s on the end of every exhausted ladder,
        # sent to an endpoint that has never heard of them. That is the failure
        # the paragraph above describes for `ollama:`, and it stopped being
        # hypothetical the moment `check_fallback_model` learned to accept a
        # fallback on another provider: a house on together.ai got
        # `[..., "deepseek-v4-flash", "glm-5.3-flash", "qwen3.8-flash"]` posted
        # to api.together.xyz.
        _first_zen = next((v for v in _wanted
                           if split_model(v)[1] == "custom"), None)
        # A bare name means "the provider this route already runs on", and
        # that is a different provider per route: `model` may be on together
        # while `subagentModel` is still on OpenCode Zen. Written bare, one
        # fallback name therefore addresses two different endpoints, and it can
        # only exist on one of them -- so the sub-agent's rescue was a together
        # model name posted at OpenCode Zen, a 404 on top of the outage it was
        # rescuing. Comparing against `everyday` hid that, because everyday is
        # the one route that happens to agree.
        #
        # So the provider is attached unless the whole thing is OpenCode Zen,
        # where bare is what it has always meant and where the OUTAGE_FALLBACKS
        # extras -- names only that gateway routes -- are appended.
        chain, seen = [], set()
        for entry in _wanted:
            ename, eprov = split_model(entry)
            # The Zen extras ride behind the first Zen entry and nowhere else.
            extras = (OUTAGE_FALLBACKS
                      if eprov == "custom" and entry == _first_zen else ())
            for candidate in ((ename, *extras) if ename else ()):
                if not candidate or candidate in seen:
                    continue
                if candidate != ename and candidate in in_use:
                    continue
                seen.add(candidate)
                chain.append(candidate if eprov == "custom"
                             else {"model": candidate, "provider": eprov})
        # A cross-provider entry is routed to that provider's own client, which
        # this config file has to declare or there is no client to build. The
        # rescue path walks past what it cannot build rather than raising --
        # correct at 3am, useless as a deploy-time answer -- so the failure was
        # silent: `nanobot-house` omits together_ai on purpose (it holds as few
        # credentials as it can), got a `together:` fallback written into it
        # anyway, and during a real OpenCode Go outage logged "Fallback provider
        # together_ai is not configured in this process" and answered the room
        # with an error. A fallback that cannot be built is a decorative
        # declaration, and this package fails those at deploy time.
        declared = set(doc.get("providers") or {})
        for entry in chain:
            if isinstance(entry, dict) and entry["provider"] not in declared:
                raise DeployError(
                    f"assistant.models.fallback runs on {entry['provider']}, "
                    f"which this assistant's config does not declare.\n"
                    f"    It would be written in and never built, so the "
                    f"rescue would be skipped at the moment it is needed.\n"
                    f"    Add a {entry['provider']!r} provider block (and pass "
                    f"its credential to that container), or name a fallback on "
                    f"one of: {', '.join(sorted(declared)) or 'nothing'}."
                )
        if chain:
            # One entry stays a plain string: the schema takes either, and a
            # house that named one model should see one model in its config.
            defaults["modelFallback"] = chain[0] if len(chain) == 1 else chain
        else:
            defaults.pop("modelFallback", None)

    # Every profession this config declares, rather than a list here that has
    # to be remembered. `doctor` and `legal` shipped in `modelProfiles` and
    # were absent from the four names this loop used to carry, so they were
    # unsettable from the site config and silently kept whatever the file said
    # -- the same shape of gap as the sub-agents above, and found the same way.
    #
    # Driven by the target file, so the house instance (which declares no
    # professions) gets none written into it, and a profession added to a
    # config is settable without editing this function.
    for role in sorted(profiles):
        if role in models:
            name, provider = split(models[role])
            profiles[role] = name
            providers[role] = provider

    # A key that is neither a direct field nor a profession this config knows
    # about reaches no container. Said out loud rather than dropped: a setting
    # that never arrives is worse than one never changed, and a typo in a
    # profession name is exactly how you get one.
    # `titles` and `documents` reach home-core and Paperless through
    # `{derived.*}`, never this config -- known so they do not read as typos.
    # `subagent_powerful` is retired and ignored, and still known: a config
    # written before 2026-09-24 has it, and that is not a typo.
    known = set(profiles) | {"everyday", "powerful", "subagent", "planner", "plan_steps",
                             "subagent_powerful", "vision", "fallback",
                             "heartbeat", "classifier", "titles", "documents"}
    unknown = sorted(k for k in models
                     if k not in known and not k.startswith("image_"))
    # Only on an instance that serves professions at all. The house one
    # declares none on purpose -- it is a room speaker, not a workspace -- so
    # warning there would list every profession on every deploy, and a warning
    # that always fires is one people learn to scroll past.
    if unknown and profiles:
        out.warn(f"assistant.models names {', '.join(unknown)}, which this "
                 f"assistant's config has no field or profession for -- the "
                 f"setting will not reach it.")

    # The same question for thinking levels, and the same answer. A role here
    # only reaches a turn through one of two routes: `powerful`, or a
    # profession this config declares -- `agent/loop.py` reads a profile's
    # effort only when that profile is in its roster. `everyday`, a sub-agent,
    # or a misspelt profession is written into the file and read by nothing.
    # `everyday` and `heartbeat` reach a turn too since 2026-09-09, through
    # `reasoningEffortDefault` and `heartbeatReasoningEffort` above -- so
    # naming them is not a dead setting, and warning about it on every deploy
    # was the always-firing warning this function is careful to avoid.
    dead_effort = sorted(k for k in effort
                         if k not in ("powerful", "everyday", "heartbeat", "classifier")
                         and k not in profiles and effort[k])
    if dead_effort and profiles:
        out.warn(f"assistant.reasoning_effort names {', '.join(dead_effort)}, "
                 f"which is neither `powerful` nor a profession this "
                 f"assistant declares -- the setting will not reach a turn.")

    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def apply_service_wiring(config_json: str, wiring: dict, cfg: dict) -> str:
    """Switch a nanobot config's skills and settings with another service.

    A skill pointed at a service nobody deployed is worse than an absent one.
    It is not merely inert: `build_skills_summary` lists skills without
    filtering on whether their environment is met, so a skill's name and its
    whole description still enter every prompt -- marked unavailable, and
    inviting the agent to try it, apologise, and spend a turn. Blanking the base URL does not help either,
    because the compose files supply it with a `:-` default and that form
    substitutes for an empty value as well as an unset one.

    So the switch is `disabledSkills`, and it is driven from the site config
    rather than from a person remembering. `home-search` off puts `searxng` and
    `vane` back on that list; on takes them off it *and* sets
    `tools.web.search.provider`, because leaving the provider at its schema
    default is what sends every ordinary lookup off the LAN -- the one thing
    running your own instance was for.

    `disable_mcp` is the same idea one level out, for a capability that is a
    *server* rather than a skill. An MCP entry left in the config is not inert
    either: nanobot connects to it at startup, and one pointed at a service the
    household never switched on is a failed connect on the path of every
    message -- this house measured 37s a message on exactly that, with the log
    saying only "failed to connect ... will retry next time". Off means the
    entry is removed from `tools.mcpServers` entirely rather than blanked,
    because a URL built from an unset `${TOKEN}` is still a URL and is still
    dialled.
    """
    if not wiring:
        return config_json

    doc = json.loads(config_json)
    services = cfg.get("services") or {}
    changed = False

    for service, rules in wiring.items():
        # `.get("enabled", True)` for the same reason every other enabled test
        # in this file uses it: a block with no `enabled:` key -- the shape the
        # admin page's services form writes -- is deployed, so the wiring has
        # to agree that the service is on rather than disabling the skills that
        # talk to a service that is running.
        on = bool((services.get(service) or {}).get("enabled", True))
        skills = list(rules.get("disable_skills") or [])
        if skills:
            defaults = doc.setdefault("agents", {}).setdefault("defaults", {})
            disabled = list(defaults.get("disabledSkills") or [])
            before = list(disabled)
            if on:
                disabled = [s for s in disabled if s not in skills]
            else:
                disabled += [s for s in skills if s not in disabled]
            if disabled != before:
                defaults["disabledSkills"] = disabled
                changed = True

        # An MCP server the household did not ask for. Removed rather than
        # left with an empty token, for the reason in the docstring: nanobot
        # dials whatever is listed.
        for server in (rules.get("disable_mcp") or ()) if not on else ():
            node = doc.get("tools")
            servers = node.get("mcpServers") if isinstance(node, dict) else None
            if isinstance(servers, dict) and server in servers:
                servers.pop(server)
                changed = True

        for dotted, value in (rules.get("set") or {}).items() if on else ():
            node = doc
            *parents, leaf = dotted.split(".")
            for part in parents:
                node = node.setdefault(part, {})
            if node.get(leaf) != value:
                node[leaf] = value
                changed = True

    if not changed:
        return config_json
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


ASSISTANT_DEFAULT_NAME = "Alfred"
_ASSISTANT_NAME_RE = re.compile(r"\bAlfred\b")


def apply_assistant_name(text: str, cfg: dict) -> str:
    """Rename the assistant in one file's contents.

    Case-sensitive and word-bounded, which is what makes this safe to run over
    source as well as prose: the capitalised `Alfred` is only ever the
    assistant's name, while the lowercase one is a path (`alfred/documents`) and
    an image (`alfred-nanobot`). Renaming those would move every file the
    assistant has saved for somebody and break the image the compose files
    name. `\b` also leaves an identifier like `AlfredThing` alone, and every
    function in the portal is `_alfred_*` anyway.
    """
    name = str((cfg.get("site") or {}).get("assistant_name")
               or ASSISTANT_DEFAULT_NAME).strip()
    if not name or name == ASSISTANT_DEFAULT_NAME:
        return text
    return _ASSISTANT_NAME_RE.sub(lambda m: name, text)


# Settings added after the first release. A config written before one existed
# must keep deploying: the manifest interpolates these names, and a missing key
# is a hard failure there -- so without this, every existing install stops
# deploying the day it pulls the commit that added the setting, with an error
# about a key its own config had no reason to carry.
#
# The value here is the old behaviour, not the recommended one. Anything that
# should make somebody choose belongs in home-stack.example.yml with a comment,
# where a new install will read it.
CONFIG_DEFAULTS = {
    "site.assistant_name": ASSISTANT_DEFAULT_NAME,
    # One resolver for the portal container to ask directly. Empty is the
    # shipped state and means "whatever the host hands out", which is right for
    # almost every house -- but it has to be *present*, because the manifest
    # exports `HOMECORE_RESOLVER: "{site.resolver}"` unconditionally and
    # `interpolate()` raises on a path the config has not got. Without this
    # line every config written before the setting existed fails
    # `deploy home-core` on "config has no 'site.resolver'", which is the
    # decorative-declaration failure in reverse: a setting nobody asked for
    # stopping a deploy of the service it belongs to.
    "site.resolver": "",
    # The household's own LAN, in CIDR. It is what tells the assistants' SSRF
    # check that `192.168.88.14` is the camera in the hall rather than
    # somewhere they are being steered into reaching.
    #
    # Empty is the shipped state and means "no exemption": every private range
    # stays blocked, which is the safe direction and what a household that
    # never sets this already gets. What it replaces is a literal
    # `192.168.1.0/24` in the nanobot config -- a shipped guess at somebody's
    # network, wrong here by one octet, and the same mistake as a default that
    # names a host. A range that is not yours is not a safety net; it is an
    # exemption granted to a network you do not own.
    "site.lan_cidr": "",
    # How long kept camera recordings stay on disk. 0 is never delete and is
    # what ships: these are `backup: bulk`, which `home-stack backup` skips
    # from any archive, so an off-box mirror is the only other copy and
    # switching this on before that mirror is known to work deletes the sole
    # copy of everything past the cutoff.
    "services.home-cameras.recording_retention_days": 0,
    # Where Home Assistant actually is, for households that do not run it in
    # this stack. Empty means "there isn't one", which is the shipped state.
    #
    # It exists because the assistants' MCP entry for Home Assistant was the
    # literal `http://homeassistant.home:8123/mcp_server/sse`, and a
    # `.home` alias is precisely what the "Names are for people" rule in
    # CLAUDE.md forbids for internal traffic. Inside the container that name is
    # NXDOMAIN, so every message paid a failed MCP connect -- measured at 37
    # seconds on this house, before a word reached the model -- and the log said
    # `failed to connect ... will retry next message` rather than naming the
    # cause. A voice turn that has to answer in a room took two minutes.
    #
    # A base URL, not a full endpoint: the `/mcp_server/sse` path stays in the
    # nanobot config next to the thing that knows it, so a value set here can
    # never be "shorter than the default" in the way NANOBOT_URL once was.
    "services.homeassistant.url": "",
    # True, not the example's false: before this setting the reservation was
    # unconditional, so an install already running the camera wall has a GPU.
    #
    # The admin page is for the household, on the household's network -- so
    # every interface, not loopback. Loopback was briefly the default and it
    # was the wrong trade: it made the page unreachable from the sofa, and the
    # answer to "who may open this" is not "only somebody already shelled into
    # the hub".
    #
    # What actually keeps it off the internet is two things, and neither is
    # this line. The proxy has no route to it -- asserted by
    # `admin_is_not_proxied()`, because that is the boundary that matters and
    # it is one Caddyfile edit away from not being true. And on a normal
    # install the hub sits behind the household's own router.
    #
    # A machine with a public address is the case that does not hold, and this
    # package cannot tell one from the outside, so `warn_admin_exposure()` says
    # what it is on every deploy and the example config says to use 127.0.0.1
    # plus `ssh -L` there. See docs/admin.md.
    "services.admin.bind": "0.0.0.0",
    # The PHP entry page had no config key at all -- a literal 8081 in its
    # compose file and in both of its checks. An install that predates the key
    # keeps the number it is already serving on, because `setdefault` never
    # moves a port under a running container; only a fresh config gets 21004.
    "services.home-core.entry_port": 8081,
    "services.home-cameras.gpu": True,
    "services.faster-whisper.device": "cuda",
    "services.faster-whisper.compute_type": "float16",
    "services.faster-whisper.model": "large-v3-turbo",
    # Which card, when on cuda. `all` is what the compose file said before it
    # had a setting, so an upgrade changes nothing.
    "services.faster-whisper.gpu_device": "all",
}


# Services that have been renamed, old name -> new. A config written before the
# rename keeps its old block; the manifest only knows the new name, so without
# this the service reads as absent and silently deploys with defaults.
SERVICE_RENAMES = {"home-web": "home-core"}


def apply_service_renames(cfg: dict) -> None:
    """Move a renamed service's config block onto its new name."""
    services = cfg.get("services")
    if not isinstance(services, dict):
        return
    for old, new in SERVICE_RENAMES.items():
        if old in services and new not in services:
            services[new] = services.pop(old)
            out.warn(f"config: services.{old} is now services.{new}; "
                     f"using it under the new name")


# Services added to the manifest after a config was written. `CONFIG_DEFAULTS`
# deliberately refuses to invent a service block that is not there -- "a service
# that is not in this config at all is not a service this deploy will touch" --
# which is right for a *setting* and wrong for a whole new service: an upgrade
# would fail --check-contract on a port for something it has never heard of and
# does not want.
#
# So a new service arrives switched off, with the values it would need if
# somebody switched it on. Nothing deploys until they do.
NEW_SERVICES = {
    "registry": {"enabled": False, "host": "hub", "port": 21040},
    # Off, but present: both nanobot units interpolate
    # `{services.home-search.port}` and `.vane_port` unconditionally, so a
    # config written before home-search existed would fail to deploy the
    # assistants -- not just the service nobody asked for.
    "home-search": {"enabled": False, "host": "compute",
                    "port": 21032, "vane_port": 21033},
    # Same shape, same reason: the per-member assistants now interpolate
    # `{services.n8n.port}` unconditionally to build NANOBOT_N8N_BASE_URL, so a
    # config predating the n8n block -- or one the admin services form rewrote
    # to a bare `{enabled: false}` -- dies on "config has no 'services.n8n.port'"
    # while deploying the assistants, not while deploying n8n.
    "n8n": {"enabled": False, "port": 21061},
    # On, unlike the three above, because that is what the example ships and
    # `.get("enabled", True)` is what every enabled test in this file reads --
    # so a config predating this block already deploys the unit. Without the
    # rest of the block it deploys it against nothing: the unit interpolates
    # `{services.crawl4ai.port}`, and the whole run dies on "config has no
    # 'services.crawl4ai.port'" before a single service is touched.
    "crawl4ai": {"enabled": True, "host": "compute", "port": 11235},
}


def add_new_services(cfg: dict) -> None:
    """Fill in a service block an older config predates, key by key.

    Key by key, not whole-block: the admin page's services form writes
    `cfg["services"].setdefault(name, {})` and then only `enabled`, so any save
    of that page turns a brand-new service into `{enabled: ...}` with no host
    and no port. A presence test then declines to fill the rest in, and every
    deploy afterwards dies on `config has no 'services.registry.port'` -- the
    `--check-contract` failure this exists to prevent, arriving by the one
    supported way to switch the service on.
    """
    services = cfg.setdefault("services", {})
    for name, block in NEW_SERVICES.items():
        existing = services.setdefault(name, {})
        for key, value in block.items():
            existing.setdefault(key, value)


def apply_config_defaults(cfg: dict) -> None:
    """Fill in settings this deployer needs that an older config predates."""
    for dotted, value in CONFIG_DEFAULTS.items():
        parts = dotted.split(".")
        node = cfg
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                # A service that is not in this config at all is not a service
                # this deploy will touch; do not invent it.
                node = None
                break
            node = nxt
        if node is not None:
            node.setdefault(parts[-1], value)


def admin_is_not_proxied(cfg: dict) -> list[str]:
    """The admin page must not be reachable through either proxy.

    This is the boundary that keeps it to the household, rather than the bind
    address: the hub is behind a router, but the proxies are deliberately
    reachable from outside -- that is what they are for -- so a route to 8099
    added to a Caddyfile or to the proxy's own router would hand the docker
    socket, the secrets file and the Deploy button to the internet, with the
    bind address still reading 0.0.0.0 and looking unchanged.

    Checked by reading the files rather than by convention, because a
    convention nobody checks is how it would come back.
    """
    port = str(((cfg.get("services") or {}).get("admin") or {}).get("port", 8099))
    problems = []
    routed = [
        ROOT / "services" / "home-core" / "proxy" / "Caddyfile",
        ROOT / "services" / "proxy" / "server" / "main.py",
        ROOT / "services" / "proxy" / "Caddyfile.snippet",
    ]
    for path in routed:
        # Unreadable is not the same as absent, and neither is a reason to
        # stop. `is_file()` raises on a path this account cannot stat, which
        # took the whole deploy down with a PermissionError out of pathlib --
        # from a *security* check, on every deploy run inside the admin
        # container, because one directory in the staged tree was mode 0700.
        #
        # And skipping it silently would be worse than the crash: this check
        # exists to prove the admin page is not proxied, so "I could not look"
        # has to read as a problem rather than as a pass.
        try:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            problems.append(
                f"{path}: cannot be read ({exc.strerror}), so it is unknown "
                f"whether it routes to the admin port {port}. That page holds "
                f"the docker socket and can deploy; an unverifiable boundary "
                f"is not a passing one.")
            continue
        for n, line in enumerate(lines, 1):
            bare = line.split("#")[0]
            if path.suffix == ".py":
                bare = line.split("#")[0]
            if port in bare and ("proxy_pass" in bare or "reverse_proxy" in bare
                                 or "api_route" in bare or "url =" in bare
                                 or "http://" in bare):
                problems.append(
                    f"{path.relative_to(ROOT)}:{n}: routes to the admin port "
                    f"{port}. The admin page is not proxied -- it holds the "
                    f"docker socket and can deploy, and the proxies answer from "
                    f"outside the house.")
    return problems


def note_vps_unmanaged(cfg: dict) -> None:
    """Say, every deploy, that the VPS exists and is not deployed from here.

    Not a warning: nothing is wrong. It is the one line that keeps a machine
    the household actually runs from quietly falling out of mind because no
    deploy ever mentions it again.
    """
    vps = (cfg.get("cloud", {}).get("vps") or {})
    if not vps.get("enabled") or vps.get("managed", True):
        return
    where = str(vps.get("host") or "").strip() or "the VPS"
    out.sub(f"{where}: enabled, and not deployed from here "
            f"(cloud.vps.managed is false).")
    out.sub("  it needs an account with a shell and docker before this stack "
            "can deploy it; see docs/proxy-home-setup.md.")


def warn_vps_is_elsewhere(cfg: dict, targets: dict) -> None:
    """Say so when `cloud.vps.host` resolves onto this network.

    The VPS is the one machine in this stack that is deliberately not here: it
    terminates the public connection and forwards everything down a tunnel to
    the house. Deploying that proxy onto a machine on the LAN is not a failure
    -- it would work, and report success, and the household's public front door
    would be a box behind their own router.

    It nearly happened. `cloud.vps.host` was a name the local resolver answered
    with a LAN address (a VPN client was intercepting the domain), so the
    deployer's idea of "the VPS" was a Raspberry Pi already running half the
    stack. The only thing that stopped it was that machine's ssh host key not
    matching -- a warning about the wrong subject, in a log, at the wrong end.

    A warning and not a refusal, because a VM on your own network is a listed
    option and somebody may mean exactly that. What it must not be is silent.
    """
    vps = (cfg.get("cloud", {}).get("vps") or {})
    if not vps.get("enabled") or not str(vps.get("host") or "").strip():
        return
    host = str(vps["host"]).strip()
    try:
        resolved = sorted({info[4][0] for info in socket.getaddrinfo(
            host, None, socket.AF_INET, socket.SOCK_STREAM)})
    except OSError:
        return                      # unresolvable is the deploy's problem, not this
    if not resolved:
        return

    def private(addr: str) -> bool:
        try:
            return ipaddress.ip_address(addr).is_private
        except ValueError:
            return False

    here = {str((h or {}).get("address") or "").strip()
            for h in (cfg.get("hosts") or {}).values()}
    clash = sorted(set(resolved) & (here - _LOOPBACK - {""}))
    if clash:
        out.warn(f"cloud.vps.host ({host}) resolves to {', '.join(clash)}, "
                 f"which is already a host in this stack.")
        out.sub("the VPS proxy would be deployed onto a machine that is "
                "already part of the house.")
    elif all(private(a) for a in resolved):
        out.warn(f"cloud.vps.host ({host}) resolves to "
                 f"{', '.join(resolved)}, which is on a private network.")
        out.sub("that is fine for a VM you run yourself, and wrong if this is "
                "meant to be a machine off this network -- a VPN client or a "
                "local resolver answering the name is the usual reason.")


def warn_admin_exposure(cfg: dict, requested: list[str]) -> None:
    """Say, on every deploy, who can reach the admin page.

    Both directions are worth a line. Loopback is the default and an upgrade
    lands on it without asking, so somebody who used to open the page over the
    LAN needs to be told where it went and how to get it back. And 0.0.0.0 is
    a real choice a household may want, but it publishes the docker socket, the
    secrets file and a deploy button to everyone on the network -- if the page
    has never been claimed, the first arrival sets the password -- so it should
    not be a thing you can forget you turned on.
    """
    admin = (cfg.get("services") or {}).get("admin")
    if not isinstance(admin, dict) or not admin.get("enabled", True):
        return
    bind = str(admin.get("bind") or "")
    port = admin.get("port", 8099)

    for problem in admin_is_not_proxied(cfg):
        out.warn(problem)

    if bind in _LOOPBACK:
        if "admin" in requested or "all" in requested:
            out.sub(f"the admin page is on {bind}:{port}, this host only. "
                    f"Reach it with `ssh -L {port}:127.0.0.1:{port} <host>`, "
                    f"or set services.admin.bind to 0.0.0.0 for the LAN.")
    elif "admin" in requested or "all" in requested:
        # Not a warning on its own. On a hub behind a router this is the LAN,
        # which is who the page is for. It is only wrong on a machine with a
        # public address, and nothing here can tell -- so say which it is and
        # let somebody who knows the answer read it.
        out.sub(f"the admin page is on {bind}:{port} -- anyone who can reach "
                f"this machine on the network can open it. That is the "
                f"household, on a hub behind a router.")
        out.sub(f"if this machine has a public address, set "
                f"services.admin.bind to 127.0.0.1 and reach it with "
                f"`ssh -L {port}:127.0.0.1:{port} <host>`.")


def compose_files_for(unit: dict, cfg: dict) -> list[str]:
    """The compose files this unit deploys with, after optional ones are settled.

    An entry may be a plain filename, or `{file: X, when: some.config.path}` for
    an overlay that only applies when that setting is on. The camera wall's GPU
    reservation is the case this exists for: `driver: nvidia` is not a hint, it
    is a hard requirement, and on a machine without one `up` fails outright with
    "could not select device driver". A GPU is an attribute of the box, so it
    cannot live in a file that ships with the service.
    """
    entries = unit.get("compose", "docker-compose.yml")
    if isinstance(entries, str):
        entries = [entries]
    files = []
    # Written here rather than by the caller, so that nothing can ask for this
    # unit's compose files and get a list that does not include one, or one
    # that is older than the config. `--check-contract`, `plan` and `deploy`
    # all arrive through this function, and the generated overlay has to be on
    # disk and current for each of them.
    generated = ensure_generated_compose(unit, cfg)
    if generated:
        entries = list(entries) + [generated]
    for entry in entries:
        if isinstance(entry, str):
            files.append(entry)
            continue
        when = entry.get("when")
        node = cfg
        for part in when.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node:
            files.append(entry["file"])
    return files


# Where the last deploy's `paths:` are remembered. In the deploy root on
# purpose: that is the one directory `paths:` cannot move, so the record
# survives the very change it exists to detect.
PATHS_MARKER = ".deployed-paths.json"


def _countable(root: Path) -> tuple[int, list[str]]:
    """(entries this account can see, directories it could not open).

    `Path.rglob` swallows a PermissionError and returns what it managed, so a
    count taken with it is silently short by whatever a service owns -- on a
    real install that is Postgres's data directory, 1858 entries of the thing
    a move most needs to carry. Counting is fine; being unable to say so is
    not.
    """
    seen, blind = 0, []
    for here, dirs, files in os.walk(root, onerror=lambda e: blind.append(
            getattr(e, "filename", str(root)))):
        seen += len(dirs) + len(files)
    return seen, blind


# The five names under `paths:`. Anything else there is not a path -- today
# that is `by_host:`, and enumerating the block without knowing the difference
# is how a role override became a directory somebody tried to create.
PATH_KINDS = ("config", "state", "media", "backups", "plugins")


def base_paths(cfg: dict) -> dict:
    """`paths:` with the per-role overrides taken out."""
    return {k: v for k, v in (cfg.get("paths") or {}).items() if k in PATH_KINDS}


def paths_for_role(cfg: dict, role: str) -> dict:
    """*cfg* as the machine behind *role* sees it.

    `paths:` is one set of directories for the whole stack, which is right
    until a role is a machine with different disks. Moving `state` onto a
    second disk on the hub told every other role to use that path too -- and
    the VPS, which has no such disk and runs one proxy, failed its deploy on
    `mkdir -p /mnt/data/...`. It was correct to fail; it was wrong to have been
    asked.

    `paths.by_host.<role>` overrides any of the five for services on that role.
    Absent, every role shares the one set, which is the single-machine install
    and stays exactly as it was.
    """
    over = ((cfg.get("paths") or {}).get("by_host") or {}).get(role) or {}
    if not over:
        return cfg
    paths = base_paths(cfg)
    paths.update({k: v for k, v in over.items() if k in PATH_KINDS})
    return {**cfg, "paths": paths}


def migrate_paths(cfg: dict, dry_run: bool = False) -> list[str]:
    """Carry state across when `paths:` changes. Returns what it did.

    Changing `paths.media` on the admin page pointed the camera wall at an
    empty directory and left 115 GB where it was -- green, with nothing to
    notice. That is the same class of loss `guard_state_paths()` exists to
    prevent, arriving from the direction it does not watch, so it is worth the
    machinery.

    Deliberately timid, because this is the most destructive thing in the
    package:

    * It moves only into a destination that is absent or empty. A non-empty one
      is two sets of state and this cannot know which is wanted.
    * It copies, verifies the file count, and only then swings the old
      directory aside to `<name>.moved-<stamp>`. Nothing is deleted, ever --
      that is a person's decision, made after they have seen it work.
    Local paths only. `paths:` is one set of directories for the whole stack,
    so on a split install the same change has to happen on each machine -- this
    carries the copy on the machine it runs on and says nothing about the
    others, which is better than pretending.

    * A file it cannot copy faithfully stops it. `/var/lib` to `/mnt/data` is a
      different filesystem, so ownership has to be recreated rather than
      inherited, and mosquitto's directory belongs to 1883 while postgres's
      belongs to 999. Half a move is worse than none.
    """
    marker = Path(os.environ.get("HOME_STACK_DEPLOY_ROOT")
                  or os.path.expanduser("~/.local/share/home-stack")) / PATHS_MARKER
    # `base_paths`, not the whole block: `by_host:` is a mapping of overrides,
    # and stringifying it would compare one dict against another as if it were
    # a directory that had moved.
    now = {k: str(v) for k, v in base_paths(cfg).items()}
    try:
        before = json.loads(marker.read_text())
    except (OSError, ValueError):
        before = {}

    # Everything that changed, checked before anything moves. A move that
    # discovers it needs root on the second of two paths has already done the
    # first, so the config, the marker and the disk disagree -- and the person
    # is told one command at a time, deploying between them to find out there
    # is another. One pass, one list, nothing touched until it can all be done.
    changed = []
    for kind, new in sorted(now.items()):
        old = before.get(kind)
        if old and old != new:
            changed.append((kind, Path(old), Path(new)))

    def by_hand_for(src: Path, dst: Path) -> str:
        # The parent first: a new path is usually somewhere that does not exist
        # yet -- that is what makes it new -- and rsync will not create a
        # missing parent. Without this the command handed to somebody who is
        # already dealing with a half-finished move fails on `mkdir ... No such
        # file or directory`.
        #
        # `<src>/ <dst>/`, and then `chown --reference`. The shorter
        # `<src> <dst-parent>/` form relies on the two leaf names being the
        # same: `paths.state` moved from `.../state` to `.../hs-state` would
        # copy into `.../state` instead, leaving the destination this deploy is
        # waiting for still empty. Trailing slashes name the destination
        # exactly; rsync then creates it owned by root under sudo, which the
        # chown puts back to whoever owns the source.
        return (f"sudo mkdir -p {shlex.quote(str(dst.parent))} && "
                f"sudo rsync -aH --numeric-ids {shlex.quote(str(src) + '/')} "
                f"{shlex.quote(str(dst) + '/')} && "
                f"sudo chown --reference={shlex.quote(str(src))} "
                f"{shlex.quote(str(dst))}")

    def nearest_existing(path: Path) -> Path:
        """The closest ancestor of *path* that is actually there.

        `os.access` on a directory that does not exist is False, so probing
        `dst.parent` refuses a perfectly ordinary move -- a second disk mounted
        at `/mnt/data` with nothing under it yet -- as "not writable", and then
        prints a sudo command that would fail too. What decides whether this
        account can create the tree is the deepest directory that exists.
        """
        here = path
        while not here.exists() and here != here.parent:
            here = here.parent
        return here

    blocked = []
    for kind, src, dst in changed:
        if not src.is_dir() or not any(src.iterdir()):
            continue
        if dst.exists() and any(dst.iterdir()):
            continue                      # already copied; checked properly below
        why = None
        anchor = dst if dst.exists() else nearest_existing(dst.parent)
        if not os.access(anchor, os.W_OK):
            why = f"{anchor} is not writable by this account"
        else:
            for root_, _dirs, files in os.walk(src, onerror=lambda e: None):
                bad = next((n for n in (_dirs + files)
                            if not os.access(os.path.join(root_, n), os.R_OK)), None)
                if bad:
                    why = f"{os.path.join(root_, bad)} is not readable by this account"
                    break
        if why:
            blocked.append((kind, src, dst, why))

    if blocked:
        lines = [f"{len(blocked)} of the paths that changed cannot be carried "
                 f"across by this account.", "Nothing has been moved."]
        for kind, src, dst, why in blocked:
            lines += ["", f"  paths.{kind}: {src} -> {dst}", f"    {why}"]
        lines += ["",
                  "A service's own files are the usual reason: the broker's "
                  "directory belongs to",
                  "uid 1883 and Postgres's to 999, and only root can read one "
                  "it does not own or",
                  "recreate that ownership on another filesystem.",
                  "", "Run these, then deploy again:", ""]
        lines += [f"  {by_hand_for(src, dst)}" for _k, src, dst, _w in blocked]
        raise DeployError("\n    ".join(lines) + "\n")

    if dry_run and changed:
        return [f"[dry-run] paths.{k}: would carry {s_} across to {d}"
                for k, s_, d in changed]

    done = []
    for kind, src, dst in changed:
        old, new = str(src), str(dst)
        if not src.is_dir() or not any(src.iterdir()):
            done.append(f"paths.{kind}: nothing at {old} to carry across")
            continue
        by_hand = by_hand_for(src, dst)
        # A destination that already holds a *complete* copy is the expected
        # state, not a conflict: the copy fails on a file this account cannot
        # read, prints the `sudo rsync` that finishes it, and says "then deploy
        # again". Refusing at that point would send somebody back to the
        # instruction they had just followed -- which this did.
        #
        # Complete means at least as many entries as the source. Fewer is a
        # half-finished copy and is refused with the rest.
        already = 0
        if dst.exists() and any(dst.iterdir()):
            # `rglob` walks as this account and silently skips a directory it
            # cannot read -- which on a real install is Postgres's, the one
            # entry whose absence would matter most. Counting both sides the
            # same blind way keeps the comparison honest: it is "as much as I
            # can see arrived as I can see", and what I cannot see is reported
            # rather than counted.
            already, blind_dst = _countable(dst)
            have, blind_src = _countable(src)
            if blind_src or blind_dst:
                out.sub(f"paths.{kind}: {len(blind_src or blind_dst)} "
                        f"directory(ies) could not be read to compare "
                        f"({', '.join((blind_src or blind_dst)[:2])})")
            if already < have:
                raise DeployError(
                    f"paths.{kind} changed from {old} to {new}, and both hold "
                    f"data.\n    {new} has {already} of the {have} entries in "
                    f"{old}, so it is neither\n    the old state nor a "
                    f"finished copy of it. Nothing was changed."
                    f"\n"
                    f"\n    Finish the copy, or empty {new}, then deploy "
                    f"again:"
                    f"\n"
                    f"\n      {by_hand}"
                    f"\n")
            out.step(f"paths.{kind}: {new} already holds all {have} "
                     f"entries from {old}")
        else:
            out.step(f"paths.{kind}: {old} -> {new}")
        # rsync will not create a missing parent, and a new path is usually
        # somewhere that does not exist yet -- that is what makes it new.
        # Guarded, because "somewhere new" is often somewhere this account does
        # not own: a second disk mounted for exactly this, whose top directory
        # belongs to root. That used to reach the operator as a PermissionError
        # traceback out of pathlib, naming the directory and nothing else.
        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DeployError(
                f"cannot create {new}: {exc.strerror}."
                f"\n    Nothing was changed. {dst.parent} belongs to another "
                f"account, so the\n    directory has to be made by one that "
                f"can write there -- and the copy has\n    to be made by one "
                f"that can read every file, which is the same command."
                f"\n"
                f"\n    Run this, then deploy again:"
                f"\n"
                f"\n      {by_hand}"
                f"\n") from exc
        # -aH --numeric-ids: ownership by number, because the uids that matter
        # here (1883, 999) may not exist as names on the target.
        rsync = ["rsync", "-aH", "--numeric-ids", f"{src}/", f"{dst}/"]
        # Not re-run over a copy somebody already finished under sudo: this
        # process cannot read those files, so it would fail on the first one
        # and undo nothing but its own progress.
        result = (subprocess.CompletedProcess(rsync, 0, "", "") if already
                  else subprocess.run(rsync, capture_output=True, text=True))
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise DeployError(
                f"could not carry paths.{kind} across:\n    "
                + "\n    ".join(detail[:3])
                + "\n    Nothing was removed and nothing was changed."
                  "\n"
                  "\n    A service's own files are the usual reason: the "
                  "broker's directory belongs\n    to uid 1883 and Postgres's "
                  "to 999, and only root can read one it does not\n    own or "
                  "recreate that ownership on another filesystem."
                  "\n"
                  # The command, not a description of it. "run the same rsync
                  # under sudo" is advice somebody then has to reconstruct,
                  # from a line they cannot see, about paths they would have to
                  # retype -- at the moment they are already dealing with a
                  # half-finished move.
                  "\n    Run this, then deploy again:"
                  f"\n"
                  f"\n      {by_hand}"
                  f"\n")
        moved = sum(1 for _ in dst.rglob("*"))
        had = sum(1 for _ in src.rglob("*"))
        if moved < had:
            raise DeployError(
                f"paths.{kind}: copied {moved} of {had} entries. Nothing was "
                f"removed;\n    the old directory is still {old}.")
        aside = src.with_name(src.name + ".moved")
        try:
            if not aside.exists():
                src.rename(aside)
                done.append(f"paths.{kind}: {had} entries carried to {new}; "
                            f"the old directory is now {aside}")
            else:
                done.append(f"paths.{kind}: {had} entries carried to {new}; "
                            f"{old} left in place ({aside} already exists)")
        except OSError:
            done.append(f"paths.{kind}: {had} entries carried to {new}; "
                        f"{old} left in place, it needs root to rename")

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
    return done


def guard_state_paths(state: list[dict], service_dir: str) -> None:
    """Refuse a state path that resolves inside the deploy directory.

    This has cost the stack four outages: the camera registry, the button and
    light storage, the dashboard's notification bridges, and the portal's
    databases were each wiped by a re-deploy because their live config sat in
    the workspace the deploy replaces. The failure is silent -- a fresh clone
    restores nothing, the container starts happily with empty state, and the
    deploy goes green.
    """
    for entry in state:
        path = Path(entry["path"])
        if not path.is_absolute():
            raise DeployError(
                f"state path {entry['path']} is relative; it must be an "
                f"absolute path outside the deploy directory"
            )
        try:
            path.relative_to(Path(service_dir))
        except ValueError:
            continue
        raise DeployError(
            f"state path {entry['path']} is inside the deploy directory "
            f"({service_dir}). A re-deploy would destroy it."
        )


def migrate_renamed_state(state: list[dict], target) -> None:
    """Move state left behind by a service that has been renamed.

    Renaming a service renames its state directory with it, and the deployer
    would otherwise create the new one empty and start the service against it.
    For `home-web` becoming `home-core` that means the portal comes up with no
    user store: a green deploy, and nobody in the house can sign in.

    Only ever a move, only when the old path exists and the new one does not,
    and it says so out loud -- so a second deploy does nothing, and one that
    does something is visible in the log rather than inferred later from
    missing data.
    """
    for entry in state:
        old = entry.get("renamed_from")
        if not old:
            continue
        new = entry["path"]
        moved = target.run(
            f"if [ -e {shlex.quote(old)} ] && [ ! -e {shlex.quote(new)} ]; then "
            f"mkdir -p {shlex.quote(str(Path(new).parent))} && "
            f"mv {shlex.quote(old)} {shlex.quote(new)} && echo moved; fi",
            check=False, capture=True)
        if (moved.stdout or "").strip() == "moved":
            out.ok(f"moved state {old} -> {new}")


def compose_volume_names(unit: dict, compose_files: list[str]) -> list[str]:
    """The named volumes a unit's compose files declare."""
    names = []
    for f in compose_files:
        path = unit_dir(unit) / f
        if not path.exists():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for name in (doc.get("volumes") or {}):
            if name not in names:
                names.append(name)
    return names


def migrate_compose_volumes(unit: dict, compose_files: list[str], legacy: str,
                            project: str, target) -> None:
    """Carry named volumes across a compose project rename.

    Compose prefixes a named volume with its project, so renaming the project
    hands the service a brand new empty volume and leaves the real one on disk
    under the old name. Nothing fails: the container starts, and the state is
    simply gone. That is how the camera registry -- `camera_registry_data`, one
    of the four wipes this deployer exists to prevent -- and the proxy's issued
    certificates would have been lost to a rename that had nothing to do with
    them.

    Only volumes this unit's own compose files declare, so a project two
    services once shared cannot pull the other's data across. Copy rather than
    move, and only into a volume that does not exist yet: a second run finds
    the destination present and does nothing.
    """
    for vol in compose_volume_names(unit, compose_files):
        old_vol, new_vol = f"{legacy}_{vol}", f"{project}_{vol}"
        moved = target.run(
            f"if docker volume inspect {shlex.quote(old_vol)} >/dev/null 2>&1 && "
            f"! docker volume inspect {shlex.quote(new_vol)} >/dev/null 2>&1; then "
            f"docker volume create {shlex.quote(new_vol)} >/dev/null && "
            f"docker run --rm -v {shlex.quote(old_vol)}:/from "
            f"-v {shlex.quote(new_vol)}:/to busybox "
            f"sh -c 'cp -a /from/. /to/ 2>/dev/null; true' >/dev/null && "
            f"echo migrated; fi",
            check=False, capture=True)
        if (moved.stdout or "").strip() == "migrated":
            out.ok(f"migrated volume {old_vol} -> {new_vol}")


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------
# A household cannot run on this package without either forking it or pushing
# its own services back into the core -- and the core has to stay clean under
# `sanitize.py --check` to remain redistributable. A plugin is the third answer:
# a directory, outside this tree, holding a `plugin.yml` whose `services:` block
# uses the same schema as deploy/manifest.yml.
#
# The core never imports, executes or knows about any particular plugin.
# Discovery, merge, done -- and the merge happens in all_services(), which is
# the one place every downstream step already reads from.

# Bumped when the shape of plugin.yml changes in a way an older deployer would
# mis-read. Refusing is the point: half-merging a plugin the deployer does not
# understand is the "green deploy, nothing happened" failure this stack is
# written against.
PLUGIN_CONTRACT = 1


def plugin_dirs(cfg: dict) -> list[Path]:
    """Resolve `plugins:` to directories.

    An entry is absolute, or relative to `paths.plugins`. That root is outside
    the package on purpose: it is where the household's own services live, and
    keeping them out of this tree is what lets `sanitize.py --check` stay clean
    on the core while a real house runs on it.
    """
    entries = cfg.get("plugins") or []
    if isinstance(entries, str):
        entries = [entries]
    root = Path(os.path.expanduser(
        str((cfg.get("paths") or {}).get("plugins")
            or "/var/lib/home-stack/plugins")))
    return [Path(os.path.expanduser(str(e))) if os.path.isabs(os.path.expanduser(str(e)))
            else root / str(e)
            for e in entries]


def load_plugins(cfg: dict) -> list[dict]:
    """Every configured plugin, or an error naming the one that failed.

    Never a silent skip. A plugin that does not load is a household service
    that will not deploy, and finding that out from a missing tile a week later
    is exactly the failure mode the rest of this file exists to prevent.
    """
    plugins = []
    seen: dict[str, Path] = {}
    for directory in plugin_dirs(cfg):
        if not directory.is_dir():
            raise DeployError(
                f"plugin directory {directory} does not exist. Remove it from "
                f"`plugins:` in config/home-stack.yml, or clone it there."
            )
        # Inside the package tree it would be swept up by sanitize.py, shipped
        # by any clone of this repo, and destroyed by a checkout. The whole
        # point of a plugin is that it lives somewhere else.
        try:
            directory.resolve().relative_to(ROOT.resolve())
        except ValueError:
            pass
        else:
            raise DeployError(
                f"plugin {directory} is inside the package tree ({ROOT}). "
                f"Plugins live outside it -- that is what keeps household data "
                f"out of the core and the core redistributable."
            )

        spec_file = directory / "plugin.yml"
        if not spec_file.exists():
            raise DeployError(f"{directory} has no plugin.yml")
        try:
            doc = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise DeployError(f"{spec_file} is not valid YAML: {exc}") from exc
        if not isinstance(doc, dict):
            raise DeployError(f"{spec_file} must be a mapping")

        contract = doc.get("contract")
        if contract is None:
            raise DeployError(
                f"{spec_file} does not declare `contract:`. Write "
                f"`contract: {PLUGIN_CONTRACT}` if it was written against this "
                f"deployer."
            )
        if not isinstance(contract, int) or contract > PLUGIN_CONTRACT:
            raise DeployError(
                f"{spec_file} declares contract {contract}; this deployer "
                f"understands up to {PLUGIN_CONTRACT}. Upgrade the package "
                f"rather than deploying a plugin it cannot read."
            )

        name = str(doc.get("name") or directory.name)
        if name in seen:
            raise DeployError(
                f"two plugins are called {name!r}: {seen[name]} and {directory}"
            )
        seen[name] = directory

        # Every unit carries where it came from, so that `dir:` resolves
        # against the plugin rather than against this repository. Stamped here,
        # once, rather than threaded through every call site that needs it.
        for svc in (doc.get("services") or {}).values():
            for unit in (svc.get("units") or []):
                unit["_root"] = str(directory)

        plugins.append({"name": name, "root": directory, "doc": doc})
    return plugins


# Skills the package ships, by directory. A plugin may not claim one of these
# names: the agent files a remote skill under the name it asked for, and remote
# outranks builtin, so the claim would hand a household service the
# instructions -- and the executed `SKILL_PYTHON.md` -- of a skill this package
# owns. `remote_skills.py` refuses the same names at run time and its set is the
# complete one (it also holds the rows in `SERVICE_SKILL_ENV` that ship no
# directory); this half exists so the answer is a failed deploy rather than a
# warning in a refresher thread nobody is watching.
SHIPPED_SKILLS_DIR = ROOT / "services" / "nanobot" / "nanobot" / "skills"

# A skill name becomes a directory name under the agent's cache. Anything that
# is not a single path component escapes it.
SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z", re.I)


# Written into every staged plugin floor so nanobot can tell one from a skill
# this package actually ships. Both sides must agree on the name; nanobot reads
# it in `agent/remote_skills.py`.
PLUGIN_FLOOR_MARKER = ".plugin-floor"


def shipped_skill_names() -> set[str]:
    try:
        return {d.name for d in SHIPPED_SKILLS_DIR.iterdir()
                if (d / "SKILL.md").is_file()}
    except OSError:
        return set()


def plugin_contributions(manifest: dict, service: str, cfg: dict) -> dict:
    """What the configured plugins add to one *core* service.

    This is the only place a plugin reaches into a service it does not own, and
    it is deliberately narrow and named rather than general. A household's own
    service can answer `GET <api>/skill` and own its assistant instructions --
    that mechanism already exists -- but the agent has to be told the address
    to ask, and the agent is nanobot, which the core ships. Nothing else in this
    deployer lets one service contribute to another, and nothing else should.

    Returns `{"env": {...}, "skills": [(name, var, rewrite)], "allowed_env_keys":
    [...], "floors": [(name, Path)]}`.
    """
    env: dict[str, str] = {}
    skills: list[tuple[str, str, str | None]] = []
    allowed: list[str] = []
    floors: list[tuple[str, Path]] = []
    claimed: dict[str, str] = {}

    for plugin in manifest.get("_plugins") or []:
        block = ((plugin["doc"].get("contributes") or {}).get(service) or {})
        if not block:
            continue
        for key, value in (block.get("env") or {}).items():
            if key in env:
                raise DeployError(
                    f"two plugins contribute {key} to {service}: "
                    f"{claimed[key]} and {plugin['name']}"
                )
            env[str(key)] = str(interpolate(value, cfg))
            claimed[str(key)] = plugin["name"]
        for row in (block.get("skills") or []):
            name, var = row.get("name"), row.get("env")
            if not name or not var:
                raise DeployError(
                    f"{plugin['name']} contributes a skill to {service} with no "
                    f"{'name' if not name else 'env'}"
                )
            if not SKILL_NAME_RE.fullmatch(str(name)):
                raise DeployError(
                    f"{plugin['name']}: skill name {name!r} is not a name. It "
                    f"becomes a directory in the agent's skill cache, so it "
                    f"may not contain a path."
                )
            if str(name) in shipped_skill_names():
                raise DeployError(
                    f"{plugin['name']}: skill {name!r} is one this package "
                    f"ships. A plugin adds a skill; it does not replace one."
                )
            skills.append((str(name), str(var), row.get("rewrite")))
            floor = row.get("floor")
            if floor:
                path = plugin["root"] / floor
                if not (path / "SKILL.md").is_file():
                    raise DeployError(
                        f"{plugin['name']}: skill floor {path} has no SKILL.md"
                    )
                floors.append((str(name), path))
        allowed += [str(k) for k in (block.get("allowed_env_keys") or [])]

    return {"env": env, "skills": skills, "allowed_env_keys": allowed,
            "floors": floors}


def skill_services_value(skills: list) -> str:
    """The `NANOBOT_SKILL_SERVICES` string remote_skills.py parses."""
    parts = []
    for name, var, rewrite in skills:
        entry = f"{name}={var}"
        if rewrite:
            entry += f":{rewrite}"
        parts.append(entry)
    return ",".join(parts)


def unit_root(unit: dict) -> Path:
    """Where a unit's `dir:` is relative to -- its plugin, or this package."""
    return Path(unit.get("_root") or ROOT)


def deploy_identity() -> list:
    """`["-i", <key>]` for the key this stack manages, or nothing.

    The admin page generates and imports a deploy key into
    `{paths.config}/ssh`, and inside that container it is `~/.ssh` -- so ssh
    finds it by default and everything worked. From a shell on the host it is
    not a default identity, so the same deploy that succeeds from the page
    fails from the command line with "Permission denied (publickey)", which
    reads as a broken key rather than as one nobody offered.

    Empty when there is no such key: an install that uses the account's own
    ~/.ssh keeps doing exactly that.
    """
    global _CFG_CACHE
    try:
        if _CFG_CACHE is None:
            # `load_yaml`, not a `load_config` -- this module has never had one,
            # so the name raised NameError, the blanket `except` below swallowed
            # it, and every call fell back to the default path. On a household
            # that has moved `paths.config` that is silently the wrong place to
            # look, which is precisely the "Permission denied (publickey)" this
            # function exists to stop.
            #
            # Cached because this is called once per ssh, scp and rsync in a
            # deploy, and re-reading and re-parsing the config each time buys
            # nothing: the key does not move mid-run.
            _CFG_CACHE = load_yaml(CONFIG) if CONFIG.exists() else {}
        base = Path((base_paths(_CFG_CACHE) or {})
                    .get("config", "/var/lib/home-stack/config"))
    except Exception:  # noqa: BLE001 - no config yet is not a reason to fail
        base = Path("/var/lib/home-stack/config")
    for name in ("id_ed25519", "id_rsa"):
        key = base / "ssh" / name
        if key.is_file():
            return ["-i", str(key)]
    return []


_CFG_CACHE = None


def unit_dir(unit: dict) -> Path:
    return unit_root(unit) / unit["dir"]


# Unit key -> the module that renders it. One entry, and the indirection is the
# point: `generated_compose: members` in the manifest says *that* a unit's
# services are rendered, and the module says how.
GENERATORS = {"members": "compose_members", "admin": "compose_admin",
              "alfred-mcp": "compose_alfred_mcp"}


_EXAMPLE_CONFIG: dict | None = None


def example_config() -> dict:
    """The shipped example config, with defaults applied and derived values
    filled in -- what a check with no household of its own renders against."""
    global _EXAMPLE_CONFIG
    if _EXAMPLE_CONFIG is None:
        cfg = load_yaml(ROOT / "config" / "home-stack.example.yml")
        apply_service_renames(cfg)
        apply_config_defaults(cfg)
        _EXAMPLE_CONFIG = derive(cfg, {})
    return _EXAMPLE_CONFIG


def generated_compose_text(unit: dict) -> str:
    """What a unit's generated compose overlay says for the example household
    with every per-person option on, without writing it anywhere. "" for a unit
    with none.

    Every option on, because the question it answers is "does anything read
    this key": the example household ticks nobody's Programmer box, and a
    bridge rendered for nobody reads nothing.
    """
    key = unit.get("generated_compose")
    if not key or key not in GENERATORS:
        return ""
    cfg = json.loads(json.dumps(example_config(), default=str))
    for member in cfg.get("members") or []:
        member.setdefault("programmer", True)
        member.setdefault("whatsapp", True)
    try:
        return _generator(GENERATORS[key]).render(cfg)
    except Exception:                                      # noqa: BLE001
        return ""


def ensure_generated_compose(unit: dict, cfg: dict) -> str | None:
    """Render this unit's generated compose overlay, and name it.

    A compose file that hardcodes one service per household member is a file
    that is wrong for every household but the one it was written for, and
    wrong quietly: compose asked to start a profile matching no service starts
    nothing and exits 0. The nanobot unit's per-member blocks are rendered from
    `services.nanobot.members` instead -- see deploy/compose_members.py.

    Imported here rather than at module scope: the renderer imports this module
    for `member_ids`, `share_folder` and the env-suffix rule, and those are the
    things it must not reimplement.
    """
    key = unit.get("generated_compose")
    if not key:
        return None
    if key not in GENERATORS:
        raise DeployError(f"unit {unit.get('name')}: no generator named {key!r}")
    return _generator(GENERATORS[key]).write(cfg, unit_dir(unit)).name


def _generator(name: str):
    """A renderer module beside this file, by path rather than by name.

    `import compose_members` needs this directory on sys.path, which it is when
    deploy.py is the script and is not when the admin page loads it from a
    file path. Same directory, either way.
    """
    import importlib.util
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed: the renderer looks through sys.modules
    # for the deployer, and this module is in there under whatever name loaded
    # it.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def plugin_services(manifest: dict) -> dict:
    """`{service name: spec}` contributed by plugins, in configured order.

    Two plugins claiming one service name is an error here rather than a
    last-one-wins overwrite. Whose service actually deployed would otherwise
    depend on the order of a list in the config file, and the one that lost
    would leave no trace at all.
    """
    services: dict[str, dict] = {}
    origin: dict[str, str] = {}
    for plugin in manifest.get("_plugins") or []:
        for name, spec in (plugin["doc"].get("services") or {}).items():
            if name in services:
                raise DeployError(
                    f"service {name!r} collides: declared by both "
                    f"{origin[name]} and {plugin['name']} "
                    f"({plugin['root']}). Rename it in one of them."
                )
            services[name] = spec
            origin[name] = plugin["name"]
    return services


def all_services(manifest: dict, cfg: dict) -> dict:
    """The deployable set: the core services, the optional ones whose feature
    is switched on, and whatever the configured plugins contribute.

    The optional ones used to be listed and undeployable -- `all` skipped them
    and naming one was an unknown-service error.

    This is the one place the set is assembled, which is why plugins merge
    here: ordering, `depends_on`, dispatch and `--check-contract` all read from
    it already, so a plugin service is indistinguishable from a core one
    everywhere downstream.
    """
    services = dict(manifest.get("services") or {})
    vps = (cfg.get("cloud", {}).get("vps") or {})
    if vps.get("enabled") and vps.get("managed", True):
        # Everything the VPS runs is the one proxy. Whether it also forwards the
        # entry page is a flag on that proxy, not a second service -- there is no
        # unit that puts application code or household data out there.
        #
        # `managed:` is a second question and not the same one. "There is a VPS"
        # and "this stack deploys it" are different facts, and collapsing them
        # into `enabled` left no way to describe the ordinary case of a machine
        # somebody set up by hand: the deployer needs a shell account there to
        # rsync a tree and run compose, and a VM provisioned with nothing but a
        # `-N` tunnel account has none. Enabled-and-failing trains a household
        # to ignore a red deploy, which is the same disease as a green one that
        # hides a wiped config -- so this says it instead.
        services.update(manifest.get("optional_services") or {})

    # `update()` would let a plugin silently take over a core service -- and a
    # shadowed `home-core` is a household's portal replaced by something a
    # deploy never mentioned. Collision is an error.
    for name, spec in plugin_services(manifest).items():
        if name in services:
            raise DeployError(
                f"plugin service {name!r} collides with a service this package "
                f"already ships. Rename it in its plugin.yml -- a plugin cannot "
                f"replace a core service."
            )
        services[name] = spec
    return services


def deploy_service(name: str, spec: dict, cfg: dict, secrets: dict,
                   targets: dict[str, Target], only: str | None,
                   remote_root: str, plugins: list | None = None) -> None:
    out.step(f"{name} - {spec.get('description', '')}")

    # Only a core service can be contributed to; a plugin extending another
    # plugin would be a dependency between two things this package does not
    # ship, and neither of them needs the core to broker it.
    contributions = plugin_contributions({"_plugins": plugins}, name, cfg)

    svc_cfg = (cfg.get("services") or {}).get(name) or {}
    # The admin page is the only way to turn a disabled service back on, so it
    # is the one service `enabled: false` does not apply to -- from the page
    # itself or from a hand-edited config.
    if name != "admin" and not svc_cfg.get("enabled", True):
        out.sub("disabled in config, skipping")
        return

    role = spec.get("role") or svc_cfg.get("host")
    target = targets.get(role)
    if target is None:
        raise DeployError(f"no host configured for role '{role}'")
    out.sub(f"target: {target}")

    # From here on, `{paths.*}` means what it means *on that machine*. A role
    # with no override sees the same dict it always did.
    scoped = paths_for_role(cfg, role)
    if scoped is not cfg:
        changed = {k: v for k, v in base_paths(scoped).items()
                   if v != base_paths(cfg).get(k)}
        cfg = scoped
        out.sub(f"{role} has its own paths: "
                + ", ".join(f"{k}={v}" for k, v in sorted(changed.items())))

    for unit in spec.get("units", []):
        unit_name = unit["name"]
        if only and unit_name != only:
            continue
        out.sub(f"-- unit: {unit_name}")

        # Per unit, not per service: two units of one service can read different
        # variables, and the proxy has no business receiving the portal's keys.
        env = collect_env(spec, unit, secrets, cfg, contributions)
        out.sub(f"{len(env)} settings supplied")

        local_dir = unit_dir(unit)
        if not local_dir.exists():
            raise DeployError(f"{unit['dir']} does not exist")
        remote_dir = f"{remote_root}/{name}/{unit_name}"

        # Interpolated first: the guard has to see the path the mount will
        # actually use, not the template it came from.
        state = interpolate(unit.get("state", []), cfg)
        guard_state_paths(state, remote_dir)
        migrate_renamed_state(state, target)

        # State directories are created before the compose run, so a bind mount
        # can never be the thing that creates them -- docker would make an empty
        # root-owned directory and the service would start with no state at all,
        # which is exactly how a green deploy hides a wiped config.
        for entry in state:
            if entry.get("kind") == "file":
                # A bind-mounted path that does not exist is created by docker
                # as a *directory*, and the container then fails to read what it
                # expects to be a file -- with an error that names neither.
                parent = str(Path(entry["path"]).parent)
                target.run(f"mkdir -p {shlex.quote(parent)}")
                probe = target.run(f"test -e {shlex.quote(entry['path'])}",
                                   check=False, capture=True)
                if probe.returncode != 0 or target.dry_run:
                    src = entry.get("seed_from_local")
                    if src and (ROOT / src).exists():
                        # Seed from what is on this machine -- the admin page's
                        # config and credentials start as copies of the ones the
                        # installer produced, then diverge as it edits them.
                        target.push_file(ROOT / src, entry["path"])
                        out.ok(f"seeded {Path(entry['path']).name} from {src}")
                    else:
                        target.run(
                            f"printf '%s' {shlex.quote(entry.get('seed', ''))} "
                            f"> {shlex.quote(entry['path'])}"
                        )
                continue

            target.run(f"mkdir -p {shlex.quote(entry['path'])}")
            # `mode:` where a service refuses a directory that is too open.
            # ssh is the one that does: it ignores a key whose directory is
            # group-readable and says so only in a log nobody is reading. Set
            # every time rather than only at creation -- a directory made by an
            # older deploy is exactly the one that has the wrong mode.
            if entry.get("mode"):
                target.run(f"chmod {shlex.quote(str(entry['mode']))} "
                           f"{shlex.quote(entry['path'])}")

            if entry.get("require_file"):
                required = f"{entry['path']}/{entry['require_file']}"
                probe = target.run(f"test -f {shlex.quote(required)}",
                                   check=False, capture=True)
                if probe.returncode != 0 and not target.dry_run:
                    seed = entry.get("seed_from")
                    seed_src = local_dir / seed if seed else None
                    if seed_src and seed_src.exists():
                        # Seed it rather than only complaining. The broker will
                        # not start against an empty config directory, and the
                        # example beside it is a working default -- refusing to
                        # copy a file we ship, and then failing on its absence,
                        # is a deploy that fails for no reason.
                        #
                        # The parent first: `require_file` may name a path a
                        # directory deep (`searxng/settings.yml`), and
                        # `push_file` is scp/copy2 -- neither creates one. The
                        # local branch raised a bare FileNotFoundError, which
                        # is not a DeployError and reached the operator as a
                        # traceback naming a file rather than the directory.
                        parent = str(Path(required).parent)
                        if parent and parent != entry["path"]:
                            target.run(f"mkdir -p {shlex.quote(parent)}")
                        target.push_file(seed_src, required)
                        out.ok(f"seeded {entry['require_file']} from {seed}")
                    else:
                        raise DeployError(
                            f"{required} does not exist on {target}." +
                            (f"\n    Copy {unit['dir']}/{seed} there and put "
                             f"real values in it." if seed else "")
                        )
            # `config_snapshots: <n>` -- pre-deploy copies of the *.json a unit
            # keeps beside its live config. Spelled `backup:` before
            # deploy/backup.py claimed that key for its per-entry policy
            # (`skip`/`bulk`/`postgres`); an int under the old name is still
            # honoured so a plugin written against it keeps working, but a
            # string belongs to the backup policy and is not a snapshot count.
            keep = entry.get("config_snapshots")
            if keep is None and isinstance(entry.get("backup"), int):
                keep = entry["backup"]
            if keep:
                backup_dir = f"{entry['path']}-backups"
                target.run(
                    f"mkdir -p {shlex.quote(backup_dir)} && "
                    f"if ls {shlex.quote(entry['path'])}/*.json >/dev/null 2>&1; then "
                    f"  stamp=$(date +%Y%m%d-%H%M%S); "
                    f"  mkdir -p {shlex.quote(backup_dir)}/$stamp && "
                    f"  cp -f {shlex.quote(entry['path'])}/*.json {shlex.quote(backup_dir)}/$stamp/; "
                    f"fi && "
                    f"ls -1t {shlex.quote(backup_dir)} 2>/dev/null | tail -n +{keep + 1} | "
                    f"while read -r old; do rm -rf {shlex.quote(backup_dir)}/$old; done",
                    check=False,
                )
                out.ok(f"live config snapshotted ({keep} kept)")

        if unit.get("member_profiles"):
            profile_dir = interpolate(unit["member_profiles"], cfg)
            target.run(f"mkdir -p {shlex.quote(profile_dir)}")
            count = 0
            for member in (cfg.get("members") or []):
                tmp = Path(tempfile.mkstemp(suffix=".md")[1])
                tmp.write_text(build_member_profile(cfg, member))
                try:
                    target.push_file(tmp, f"{profile_dir}/{member['id']}.md")
                finally:
                    tmp.unlink(missing_ok=True)
                count += 1
            out.ok(f"{count} member profile(s) generated")

        # The vhost block for the VPS's own Caddy, with the household's own
        # domain in it. Written beside the service so it is on the machine that
        # needs it; installing it is left to a person, because /etc/caddy
        # belongs to the machine rather than to this stack.
        if unit.get("vps_caddyfile"):
            # The tree is pushed further down, so on a machine this stack has
            # never deployed to there is nowhere to put this yet -- scp said
            # `dest open ...: No such file or directory` and the deploy stopped
            # before it had done anything. Invisible on any host with a
            # previous deploy behind it, which is every host anybody tested on.
            target.run(f"mkdir -p {shlex.quote(remote_dir)}")
            tmp = Path(tempfile.mkstemp(suffix=".caddy")[1])
            tmp.write_text(build_vps_caddyfile(cfg), encoding="utf-8")
            try:
                target.push_file(tmp, f"{remote_dir}/{unit['vps_caddyfile']}")
            finally:
                tmp.unlink(missing_ok=True)
            domain = ((cfg.get("cloud") or {}).get("vps") or {}).get("domain")
            if domain:
                out.ok(f"vhost for {domain} written to "
                       f"{unit['vps_caddyfile']} - append it to "
                       f"/etc/caddy/Caddyfile and reload")
            else:
                out.warn("cloud.vps.domain is empty, so the proxy serves no "
                         "name of its own and gets no certificate")

        # The dashboard's tiles. Written before the container starts, because
        # the compose file bind-mounts this path as a *file*: if it does not
        # exist when the mount is made, the daemon creates a directory there
        # and the portal opens a directory where it expects JSON. Mounting it
        # read-only is deliberate -- the deployer owns this file, and a portal
        # that could edit it would be editing something the next deploy
        # overwrites.
        if unit.get("family_directory"):
            dest = interpolate(unit["family_directory"], cfg)
            target.run(f"mkdir -p {shlex.quote(str(Path(dest).parent))}")
            target.run(f"test -d {shlex.quote(dest)} && "
                       f"rmdir {shlex.quote(dest)} || true", check=False)
            rendered = build_family_directory(cfg)
            tmp = Path(tempfile.mkstemp(suffix=".json")[1])
            tmp.write_text(rendered, encoding="utf-8")
            try:
                target.push_file(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            out.ok(f"{len(json.loads(rendered)['people'])} member(s) written to "
                   f"the family directory")

        # `opencode_servers()` is the same question HomeCore is answered with,
        # so the two cannot disagree: empty here is exactly when OPENCODE_SERVERS
        # is empty there and every Programmer falls back to the assistant.
        # Writing anyway -- `cloud.opencode` off, or nobody ticked -- would put
        # every provider credential this household has onto the host and start
        # an `opencode serve` per ticked member, an agent with a shell behind
        # nothing but loopback, to back a feature nothing is routed to.
        if unit.get("opencode_config") and not opencode_servers(cfg):
            out.sub("cloud.opencode serves nobody, so no opencode is "
                    "configured and every Programmer runs on the assistant")
        elif unit.get("opencode_config"):
            # One opencode per member: its own config directory, its own agent,
            # its own MCP token, its own port, its own systemd instance.
            #
            # One server for the household is not an option, and not for
            # scale: opencode holds a single MCP block, so a shared server
            # would authenticate every member's tools as one person. The
            # profession spaces are open to everybody, so that is a second
            # member getting the first one's projects and share with nothing
            # failing.
            #
            # The restart is not tidiness either. opencode reads all of this
            # **at startup and never again** -- measured: an agent file added
            # to a running server does not appear in /agent, and an edited
            # prompt still serves the old text.
            root = interpolate(unit["opencode_config"], cfg)
            chosen = opencode_model(cfg)
            base = opencode_port_base(cfg)
            gen = _generator(GENERATORS["alfred-mcp"])
            agents = sorted((ROOT / "deploy/host/opencode/agents").glob("*.md"))

            cfgroot = target.run(
                'echo "${XDG_CONFIG_HOME:-$HOME/.config}"',
                capture=True).stdout.strip()
            target.run(f"mkdir -p {shlex.quote(cfgroot)}/home-stack")

            # The provider credentials, shared by every member's server: they
            # are the household's, not one person's. opencode reads them from
            # the environment and works the providers out on its own, which is
            # why no `provider` block is written and its config carries no
            # secrets.
            #
            # Only keys that are set. An exported empty one makes opencode
            # offer a provider that answers 401 on the first turn, which reads
            # as the model being broken rather than the credential being absent.
            lines = ["# Written by ./home-stack deploy. Provider credentials "
                     "for opencode.",
                     "# opencode discovers providers from these on its own; "
                     "its config carries no keys."]
            for key in OPENCODE_PROVIDER_KEYS:
                value = (secrets.get(key) or "").strip()
                if value:
                    lines.append(f"{key}={value}")
            tmp = Path(tempfile.mkstemp(suffix=".env")[1])
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                dest_env = f"{cfgroot}/home-stack/opencode-providers.env"
                target.push_file(tmp, dest_env)
                target.run(f"chmod 600 {shlex.quote(dest_env)}")
            finally:
                tmp.unlink(missing_ok=True)
            out.ok(f"{len(lines) - 2} provider credential(s) given to opencode")

            # The checkout root, for the one host unit that needs it and is not
            # per member: opencode-preview-reaper.timer, which stops preview
            # servers a failed turn left running. Written here because this is
            # where the value is known -- the installer that enables the timer
            # has no config to read it from.
            ws_root = (base_paths(cfg) or {}).get("state", "")
            if ws_root:
                tmp = Path(tempfile.mkstemp(suffix=".env")[1])
                tmp.write_text(
                    "# Written by ./home-stack deploy. The directory the code\n"
                    "# broker checks projects out into; the reaper looks at\n"
                    "# nothing outside it.\n"
                    f"OPENCODE_WORKSPACE_ROOT={ws_root.rstrip('/')}"
                    "/nanobot-code-workspace\n", encoding="utf-8")
                try:
                    dest_ws = f"{cfgroot}/home-stack/opencode-workspace.env"
                    target.push_file(tmp, dest_ws)
                finally:
                    tmp.unlink(missing_ok=True)

            started, failed, unchanged = [], [], []
            for index, member in enumerate(gen.members(cfg)):
                mroot = f"{root}/{member}"
                target.run(f"mkdir -p {shlex.quote(mroot)}/agents")
                # Everything this member's server reads at startup, hashed, so a
                # deploy that changes none of it does not restart the server.
                #
                # The restart is not free and it is not invisible: opencode holds
                # the Programmer's turn, so restarting it kills whatever that
                # person is in the middle of -- the event stream dies, the answer
                # never arrives, and the chat is left showing a question with no
                # reply. That happened here, four times in one afternoon, to
                # deploys that wrote byte-identical configuration.
                digest = hashlib.sha256()

                rendered = build_opencode_config(
                    member=member,
                    token=env.get(
                        f"ALFRED_MCP_TOKEN_{member_env_suffix(member)}", ""),
                    port=gen.port_for(cfg, member),
                    # Where this member's checkouts live, as the host sees them
                    # -- which is where opencode runs. Empty if the config has
                    # no `paths:` at all, and then no permission is granted
                    # rather than one naming the wrong directory.
                    workspace=(
                        f"{(base_paths(cfg) or {}).get('state', '').rstrip('/')}"
                        "/nanobot-code-workspace"
                        if (base_paths(cfg) or {}).get("state") else ""))
                digest.update(rendered.encode("utf-8"))
                tmp = Path(tempfile.mkstemp(suffix=".json")[1])
                tmp.write_text(rendered, encoding="utf-8")
                try:
                    target.push_file(tmp, f"{mroot}/opencode.json")
                    # It carries the token that opens this member's bridge.
                    target.run(f"chmod 600 {shlex.quote(mroot)}/opencode.json")
                finally:
                    tmp.unlink(missing_ok=True)

                for src in agents:
                    body = apply_household_language(
                        src.read_text(encoding="utf-8"), cfg)
                    # opencode's own per-agent model selection. The line is
                    # dropped rather than left empty when no model is named:
                    # `model:` with nothing after it is a parse error, and an
                    # agent that fails to parse is one opencode does not list --
                    # which the portal reads as "not ready" and quietly answers
                    # on the assistant instead.
                    body = (body.replace("{{OPENCODE_MODEL}}", chosen) if chosen
                            else re.sub(r"^model: \{\{OPENCODE_MODEL\}\}\n", "",
                                        body, flags=re.M))
                    digest.update(src.name.encode("utf-8"))
                    digest.update(body.encode("utf-8"))
                    tmp = Path(tempfile.mkstemp(suffix=".md")[1])
                    tmp.write_text(body, encoding="utf-8")
                    try:
                        target.push_file(tmp, f"{mroot}/agents/{src.name}")
                    finally:
                        tmp.unlink(missing_ok=True)

                # Where this member's unit finds its port and its config. The
                # unit itself is a template instantiated per member, so this is
                # the only per-member thing it has to be told.
                instance_env = (
                    f"# Written by ./home-stack deploy for {member}.\n"
                    f"OPENCODE_SERVE_PORT={base + index}\n"
                    f"OPENCODE_CONFIG={mroot}/opencode.json\n"
                    f"OPENCODE_CONFIG_DIR={mroot}\n")
                digest.update(instance_env.encode("utf-8"))
                inst = Path(tempfile.mkstemp(suffix=".env")[1])
                inst.write_text(instance_env, encoding="utf-8")
                try:
                    target.push_file(
                        inst,
                        f"{cfgroot}/home-stack/opencode-serve-{member}.env")
                finally:
                    inst.unlink(missing_ok=True)

                # `--user`, and best effort. A household that has not installed
                # the units yet is not a failed deploy: the Programmer simply
                # stays on the assistant, which is what the portal's own
                # readiness check already decides.
                # `enable` then `restart`, and both are needed. `enable --now`
                # does not restart a unit that is already running, so it would
                # leave the old configuration serving; `restart` alone starts
                # it now and never again after a reboot. Enabling is idempotent.
                target.run(f"systemctl --user enable opencode-serve@{member}",
                           check=False)

                # Restart only when something this server reads at startup
                # actually changed, or when it is not running. The marker holds
                # the digest of what was last written; `is-active` is asked as
                # well, because a matching marker in front of a stopped server
                # would mean never starting it again.
                marker = f"{mroot}/.config-digest"
                want = digest.hexdigest()
                seen = target.run(f"cat {shlex.quote(marker)} 2>/dev/null || true",
                                  capture=True, check=False)
                live = target.run(
                    f"systemctl --user is-active --quiet opencode-serve@{member}",
                    check=False)
                same = (getattr(seen, "stdout", "").strip() == want
                        and getattr(live, "returncode", 1) == 0)
                if same:
                    unchanged.append(member)
                    continue

                restarted = target.run(
                    f"systemctl --user restart opencode-serve@{member}",
                    check=False)
                ok_now = getattr(restarted, "returncode", 1) == 0
                (started if ok_now else failed).append(member)
                # Written after the restart, not before: a marker recorded for a
                # restart that failed would tell the next deploy there was
                # nothing to do, and the server would go on serving the old
                # configuration with nothing saying so.
                if ok_now:
                    target.run(
                        f"printf %s {shlex.quote(want)} > {shlex.quote(marker)}",
                        check=False)

            if started:
                out.ok(f"opencode config and {len(agents)} agent(s) written for "
                       f"{', '.join(started)}; restarted")
            if unchanged:
                out.ok(f"opencode unchanged for {', '.join(unchanged)}; left "
                       f"running")
            if failed:
                out.warn(
                    f"opencode-serve did not restart for {', '.join(failed)} -- "
                    f"it reads its config only at startup, so the Programmer "
                    f"keeps whatever it had. "
                    f"{_opencode_restart_remedy(target)}")

        if unit.get("portal_dashboard"):
            dest = interpolate(unit["portal_dashboard"], cfg)
            target.run(f"mkdir -p {shlex.quote(str(Path(dest).parent))}")
            # The directory docker leaves behind on a first deploy, cleared
            # here because this file is the deployer's own and nothing else
            # writes it. `rmdir`, not `rm -rf`: it removes exactly the empty
            # directory that is the symptom and refuses anything else, so a
            # household that put something at this path keeps it and gets the
            # error from push_file instead of losing it.
            target.run(f"test -d {shlex.quote(dest)} && "
                       f"rmdir {shlex.quote(dest)} || true", check=False)
            rendered = build_portal_dashboard(cfg, plugins)
            tmp = Path(tempfile.mkstemp(suffix=".json")[1])
            tmp.write_text(rendered, encoding="utf-8")
            try:
                target.push_file(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            tiles = sum(len(g) for g in
                        json.loads(rendered)["groups"].values())
            out.ok(f"{tiles} dashboard tile(s) generated")

        for net in unit.get("pre_create_networks", []):
            # `docker compose config` does not verify that an external network
            # exists; `up` aborts on a fresh host. Creating it is idempotent.
            target.run(
                f"docker network inspect {shlex.quote(net)} >/dev/null 2>&1 || "
                f"docker network create {shlex.quote(net)}"
            )
            out.ok(f"network {net} present")

        # The generated overlay, rendered from *this* config before the tree is
        # staged or pushed. compose_files_for() renders it too, but that runs
        # after the push -- so every deploy shipped whatever file the checkout
        # held beforehand. On 2026-09-12 that was a two-member file a test run
        # had left there: `up --remove-orphans` removed three members'
        # assistants and a WhatsApp bridge, the deploy said "5 member
        # profile(s) generated" and went green, and the correct file was
        # rendered into the checkout a moment too late to matter. The same
        # order meant a newly added member got a container only on the
        # *second* deploy.
        ensure_generated_compose(unit, cfg)

        assets = unit.get("assets", [])
        # `model_config:` names a JSON file inside this unit's tree whose model
        # choices come from `assistant.models`. It is patched in the staging
        # copy, never in the checkout: the file in git is the documented
        # default, and a deploy that rewrote it would turn every deploy into a
        # dirty working tree.
        model_files = unit.get("model_config") or []
        if isinstance(model_files, str):
            model_files = [model_files]

        # A plugin's floor copy of the skill its service serves. It goes into
        # the *builtin* skills directory, which SkillsLoader consults last:
        # workspace beats remote beats builtin, so a floor copy in the workspace
        # would permanently defeat the live version the service is serving.
        floors = (contributions.get("floors") or []) if unit.get("skill_floors") else []

        # Contributed variables have to reach the *container*, not just the
        # interpolation. A name the deployer exports but no compose service
        # lists arrives nowhere -- that is how the Ollama endpoints once did --
        # and the core compose cannot name a household service's variables, so
        # they go in a file its services read with an optional `env_file:`.
        contributed_env = dict((contributions.get("env") or {}))
        # Per-member notification topics. A dynamic set of names cannot be
        # listed in a static compose file, which is the whole reason the
        # env_file exists -- the same escape hatch plugin contributions use.
        if unit.get("member_ntfy_topics"):
            contributed_env.update(member_notification_topics(cfg))
        if contributions.get("skills"):
            contributed_env["NANOBOT_SKILL_SERVICES"] = skill_services_value(
                contributions["skills"])
        wants_env_file = bool(contributed_env) and unit.get("contributions_env_file")

        if (assets or model_files or floors or wants_env_file
                or unit.get("when_service") or unit.get("assistant_name_in")):
            staging = Path(tempfile.mkdtemp(prefix="home-stack-"))
            try:
                shutil.copytree(local_dir, staging / "src", symlinks=True,
                                ignore=shutil.ignore_patterns(
                                    ".git", "__pycache__", "node_modules", ".venv"))
                # The assistant's own name, in whatever this unit declares
                # mentions it. Done in the staging copy for the same reason the
                # model choices are: the checkout stays the documented default.
                if wants_env_file:
                    lines = [
                        "# Written by deploy/deploy.py from the `contributes:`",
                        "# blocks of the configured plugins. Do not edit: it is",
                        "# rewritten on every deploy.",
                    ]
                    for key in sorted(contributed_env):
                        lines.append(f"{key}={contributed_env[key]}")
                    (staging / "src" / unit["contributions_env_file"]).write_text(
                        "\n".join(lines) + "\n", encoding="utf-8")
                    out.ok(f"{len(contributed_env)} contributed variable(s) staged")

                for skill_name, floor in floors:
                    dest = staging / "src" / unit["skill_floors"] / skill_name
                    if dest.exists():
                        raise DeployError(
                            f"a plugin's floor skill {skill_name!r} would "
                            f"overwrite one this package ships"
                        )
                    shutil.copytree(floor, dest)
                    # Mark it, or the two halves of this feature refuse each
                    # other. A floor is staged into the *builtin* skills
                    # directory on purpose -- SkillsLoader reads workspace,
                    # then remote, then builtin, so this is the only tier where
                    # the live copy the service serves still wins. But nanobot
                    # also derives "skills this package ships" from that same
                    # directory, and it does so at runtime, after the staging
                    # this loop just did. So the floor made the remote entry
                    # look like a name collision and it was dropped -- leaving
                    # the floor as the only copy, which is the exact opposite
                    # of what a floor is for.
                    #
                    # The two collision checks that matter both run before
                    # this: `plugin_contributions` refuses a plugin skill named
                    # after a shipped one, and `dest.exists()` above refuses a
                    # floor that would overwrite one. By the time a marker is
                    # written, the name is known not to be a shipped skill's.
                    (dest / PLUGIN_FLOOR_MARKER).write_text(
                        "Written by deploy/deploy.py. This directory is a "
                        "plugin's floor copy, not a skill this package ships; "
                        "the live one is served by the plugin's own service.\n",
                        encoding="utf-8")
                if floors:
                    out.ok(f"{len(floors)} plugin skill floor(s) staged")

                renamed = 0
                for pattern in (unit.get("assistant_name_in") or []):
                    for path in sorted((staging / "src").glob(pattern)):
                        if not path.is_file():
                            continue
                        original = path.read_text(encoding="utf-8")
                        updated = apply_assistant_name(original, cfg)
                        if updated != original:
                            path.write_text(updated, encoding="utf-8")
                            renamed += 1
                if renamed:
                    who = (cfg.get("site") or {}).get("assistant_name")
                    out.ok(f"assistant renamed to {who} in {renamed} file(s)")

                # The wiring is applied to the same files `model_config:` names,
                # so declaring one without the other is a switch that never
                # reaches a container -- green, silent, and exactly the
                # decorative-declaration failure `--check-contract` exists for.
                if unit.get("when_service") and not model_files:
                    raise DeployError(
                        f"{name}/{unit['name']}: when_service: needs a "
                        f"model_config: to write into")
                for rel in model_files:
                    target_file = staging / "src" / rel
                    if not target_file.is_file():
                        raise DeployError(
                            f"model_config names {rel}, which is not in {unit['dir']}")
                    patched = apply_model_choices(
                        target_file.read_text(encoding="utf-8"), cfg)
                    patched = allow_env_keys(
                        patched, contributions.get("allowed_env_keys") or [])
                    # Skills and settings that follow another service's enabled
                    # state. Same staging copy and the same reason: the file in
                    # git stays the documented default.
                    patched = apply_service_wiring(
                        patched, unit.get("when_service") or {}, cfg)
                    patched = apply_household_language(patched, cfg)
                    target_file.write_text(patched, encoding="utf-8")
                # The prompt files beside it. They are shipped English and
                # redistributable, so the language cannot be written into them
                # -- it is this household's, filled in on the way to the
                # container. Only files that carry the token are rewritten.
                for prompt_file in sorted((staging / "src").rglob("*.md")):
                    before = prompt_file.read_text(encoding="utf-8")
                    after = apply_household_language(before, cfg)
                    if after != before:
                        prompt_file.write_text(after, encoding="utf-8")
                if model_files:
                    chosen = (cfg.get("assistant") or {}).get("models") or {}
                    out.ok(f"{len(chosen)} model choice(s) applied to "
                           f"{len(model_files)} config file(s)")
                for asset in assets:
                    src = ROOT / asset["from"]
                    if not src.exists():
                        raise DeployError(f"asset {asset['from']} does not exist")
                    dst = staging / "src" / asset["to"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        # The same exclusions a push uses. Without them an
                        # asset carries .git, every __pycache__, and -- the one
                        # that matters -- any `.env` or `secrets/` under the
                        # tree, straight into an image layer.
                        shutil.copytree(
                            src, dst, dirs_exist_ok=True, symlinks=True,
                            ignore=shutil.ignore_patterns(*TREE_EXCLUDES))
                    else:
                        shutil.copy2(src, dst)
                out.ok(f"{len(assets)} asset(s) staged into the build context")
                target.push(staging / "src", remote_dir)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        else:
            target.push(local_dir, remote_dir)

        # A unit may stack an override on a base file -- `compose:` accepts a
        # list. The local proxy's file is an override and was deployed alone,
        # which is a service with no image and no build: undeployable, and
        # nothing said so until `up`.
        compose_files = compose_files_for(unit, cfg)
        # Compose profiles, from the members the config actually lists. The
        # per-member services are gated on these: a compose file that hardcodes
        # five instances would otherwise start all five on a two-person
        # install, three of them with no API secret and nothing to do but
        # restart-loop.
        if unit.get("member_profiles_gate"):
            # From the renderer, so that what is asked for and what exists are
            # the same list. A profile compose does not know is not an error:
            # it starts nothing, says nothing and exits 0.
            wanted = _generator("compose_members").profiles(cfg)
            if wanted:
                env["COMPOSE_PROFILES"] = ",".join(wanted)

        files_flags = " ".join(f"-f {shlex.quote(f)}" for f in compose_files)

        # Compose names a project after its directory unless told otherwise,
        # and the directory here is the *unit* name. Two services with a unit
        # called `local` therefore shared one project -- and every `up` runs
        # `--remove-orphans`, which deletes containers in the project that the
        # current files do not define. Deploying the portal destroyed the chat
        # proxy and deploying the chat proxy destroyed the portal, each one
        # reporting success. `<service>-<unit>` is unique by construction.
        project = unit.get("compose_project") or f"{name}-{unit['name']}"
        # Into `env`, not only onto the compose command line. A `pre:` script
        # that touches a compose-managed resource has to name it the same way
        # compose does, and the only way it can know the project is to be told:
        # local-cert.sh guessed `basename $PWD` -- correct until the rename
        # above made the project `<service>-<unit>` -- and wrote the proxy's
        # certificate into `proxy_proxy-certs` while Caddy mounted
        # `home-core-proxy_proxy-certs`. Caddy then crash-looped on a missing
        # /certs/fullchain.pem that had in fact been generated, one volume over.
        env["COMPOSE_PROJECT_NAME"] = project
        compose = (f"COMPOSE_PROJECT_NAME={shlex.quote(project)} "
                   f"docker compose {files_flags}")

        # One-time: containers created under the old bare-unit-name project are
        # invisible to the new one, so they keep running and hold the ports and
        # names the new ones need. Scoped by -f to this unit's own services, so
        # it can only remove what this unit created.
        # Only for units this package shipped. The legacy project name is the
        # bare unit name, which for a plugin is a name this deployer has never
        # used -- and `down` on it would reach for whatever unrelated project a
        # household happens to have called `app` or `web`. A plugin has no
        # history here to migrate.
        legacy = unit["name"] if not unit.get("_root") else project
        if legacy != project:
            target.run(f"COMPOSE_PROJECT_NAME={shlex.quote(legacy)} "
                       f"docker compose {files_flags} down --remove-orphans",
                       cwd=remote_dir, env=env, check=False)
            migrate_compose_volumes(unit, compose_files, legacy, project, target)

        target.run(f"{compose} config -q", cwd=remote_dir, env=env)
        out.ok("compose file valid")

        # Tests run against the built image before it replaces the running one,
        # so a red run stops the house getting the change rather than reporting
        # on it afterwards.
        test = unit.get("test")
        if test and unit.get("build", False):
            target.run(f"{compose} build", cwd=remote_dir, env=env)
            out.ok("image built")
        if test:
            if test.get("host"):
                cmd = " ".join(shlex.quote(c) for c in test["command"])
            else:
                mount = test.get("mount", {})
                mount_flag = (
                    f"-v {shlex.quote(remote_dir + '/' + mount['from'])}:{mount['to']}:ro"
                    if mount else ""
                )
                cmd = (
                    f"docker run --rm {mount_flag} {shlex.quote(test['image'])} "
                    + " ".join(shlex.quote(c) for c in test["command"])
                )
            result = target.run(cmd, cwd=remote_dir, env=env, check=False, capture=True)
            if result.returncode != 0 and not target.dry_run:
                raise DeployError(
                    f"tests failed before deploy; the running service was not "
                    f"touched\n    {(result.stdout or '')[-800:]}"
                )
            out.ok("tests passed")

        for container in unit.get("pre_remove_containers", []):
            # Container names are global, not project-scoped: one created by
            # hand or under another project name is invisible to `down` and
            # makes `up` fail on a name conflict.
            target.run(f"docker rm -f {shlex.quote(container)} 2>/dev/null || true",
                       cwd=remote_dir, check=False)

        # One image, built by name, before compose runs. Not the same thing as
        # `build: true`: that maps to `docker compose build`, which builds what
        # a *service* declares. These compose files deliberately declare no
        # per-service `build:` — five services sharing one image would have
        # Compose build the same Dockerfile five times and export five
        # byte-identical copies — so nothing built it at all and `up` fell
        # through to `docker pull alfred-nanobot`, which is not a registry
        # image and never will be. A shell script used to do this; deleting it
        # took the build with it.
        # A list, when a unit builds more than one image out of one context --
        # a runtime image and the test stage beside it. One entry stays one
        # entry; nothing that declared a single image has to change.
        builds = unit.get("image_build")
        for spec_img in (builds if isinstance(builds, list) else
                         ([builds] if builds else [])):
            tag = spec_img["tag"] if isinstance(spec_img, dict) else str(spec_img)
            context = (spec_img.get("context", ".") if isinstance(spec_img, dict) else ".")
            # A named stage, for a Dockerfile that keeps its test tooling out
            # of the runtime image. Without it the only way to run a service's
            # own suite against the thing being deployed is to ship pytest to
            # the house.
            stage = (spec_img.get("target") if isinstance(spec_img, dict) else None)
            staged = f"--target {shlex.quote(stage)} " if stage else ""
            target.run(
                f"docker build {staged}-t {shlex.quote(tag)} {shlex.quote(context)}",
                cwd=remote_dir, env=env)
            out.ok(f"image {tag} built")

        if unit.get("pre"):
            target.run(unit["pre"], cwd=remote_dir, env=env)
            out.ok(f"{unit['pre']} done")

        if unit.get("pull"):
            target.run(f"{compose} pull", cwd=remote_dir, env=env)

        up = f"{compose} up -d --remove-orphans"
        if unit.get("build", False):
            up += " --build"
        target.run(up, cwd=remote_dir, env=env)
        out.ok("containers up")

        if unit.get("verify"):
            verify(target, unit["verify"], cfg, env, remote_dir)


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------

def order_services(names: list[str], manifest: dict) -> list[str]:
    """Topological order over `depends_on`, stable otherwise."""
    services = manifest["services"]
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(name: str, trail: tuple[str, ...] = ()) -> None:
        if name in seen:
            return
        if name in trail:
            raise DeployError(
                "dependency cycle: " + " -> ".join(trail + (name,))
            )
        for dep in services.get(name, {}).get("depends_on", []):
            if dep in services:
                visit(dep, trail + (name,))
        seen.add(name)
        ordered.append(name)

    for name in names:
        visit(name)
    return ordered


# --------------------------------------------------------------------------

COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?][^}]*)?\}")


def compose_refs(text: str) -> list[tuple[str, str]]:
    """`(name, modifier)` for every `${VAR}` in *text*, nested ones included.

    `COMPOSE_VAR` stops at the first `}`, so in `${A:-${B:-x}/v1}` it sees A
    with a modifier of `:-${B:-x` and never B. Compose resolves both, so a
    check that reads only the outer name misses a variable the container
    really receives -- the inner one is scanned out of the modifier here.
    """
    out = []
    for name, rest in COMPOSE_VAR.findall(text):
        out.append((name, rest))
        if "${" in rest:
            out.extend(compose_refs(rest + "}"))
    return out


def compose_variables(path: Path) -> dict[str, str]:
    """Every ${VAR} a compose file reads, and how it behaves when unset.

    `bare` resolves to the empty string, which is the dangerous case: an empty
    credential fails as an outage and an empty path can fall back to somewhere
    inside the deploy tree. `default` and `guarded` are self-describing.
    """
    # Comments are stripped first: a `${VAR}` inside a comment explaining why
    # something is *not* a variable any more was reported as unsupplied.
    text = "\n".join(
        re.sub(r"(?<!\$)#.*$", "", line) for line in path.read_text().splitlines()
    )
    kinds: dict[str, str] = {}
    for name, rest in compose_refs(text):
        kind = "guarded" if rest.startswith((":?", "?")) else ("default" if rest else "bare")
        # Worst case wins if one file uses a name both ways.
        if kinds.get(name) != "bare":
            kinds[name] = kind
    return kinds


def compose_service_variables(svc: dict) -> set[str]:
    """Every `${VAR}` one compose *service* reads, anywhere in its block.

    Parsed rather than grepped, for the reason `compose_variables` strips
    comments: a name mentioned in a comment explaining why it is *not* read any
    more is not read. Per service, because which container receives a variable
    is the whole question here -- `environment:` on the database says nothing
    about the container doing the dialling.

    `ports:` and `expose:` are left out on purpose. A variable there is an
    address on the *host* -- `ADMIN_BIND` is `127.0.0.1` so the admin page is
    published to this machine and nowhere else, which is the point of it -- and
    reading it as somewhere the container dials would demand an exemption for
    getting it right.
    """
    block = {k: v for k, v in svc.items() if k not in ("ports", "expose")}
    text = yaml.safe_dump(block, default_flow_style=False, allow_unicode=True)
    return {name for name, _ in compose_refs(text)}


def compose_host_aliases(svc: dict) -> set[str]:
    """The names in one service's `extra_hosts:`, list or mapping form."""
    entries = svc.get("extra_hosts") or []
    if isinstance(entries, dict):
        return {str(k) for k in entries}
    return {str(e).split(":", 1)[0].strip() for e in entries if isinstance(e, str)}


def compose_env_files(svc: dict) -> set[str]:
    """The `env_file:` paths one service reads, in any of compose's spellings."""
    entries = svc.get("env_file") or []
    if isinstance(entries, str):
        entries = [entries]
    names = set()
    for entry in entries:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict) and entry.get("path"):
            names.add(str(entry["path"]))
    return names


def is_loopback_address(value: str) -> bool:
    """Is this value an address that means "me"?

    Both spellings the manifest can produce: a bare host (`MQTT_BROKER`,
    `FILE_SHARE_HOST`, `SMB_HOST` -- the conventions in docs/mqtt-conventions.md
    are host-only on purpose) and a URL of any scheme, `mqtt://` and `ws://`
    included. Anchored on `_LOOPBACK` so there is one list rather than one per
    check -- an earlier version matched `https?://` only, and the bare hosts
    beside the URLs it did catch had the identical bug with no diagnostic.
    """
    text = value.strip()
    if text.lower() in _LOOPBACK:
        return True
    if "://" not in text:
        return False
    try:
        host = urlsplit(text).hostname
    except ValueError:          # a port that is not a number, and not ours
        return False
    return bool(host) and host.lower() in _LOOPBACK


def loopback_reachability_problems(services: dict, cfg: dict,
                                   manifest: dict | None = None) -> list[str]:
    """A bridge-networked container handed a loopback address reaches nothing.

    On the default one-PC install every role is 127.0.0.1. On the host that is
    right -- ssh, rsync and a verify `curl` all mean this machine -- and for a
    service on host networking it is right too. Inside a bridge-networked
    container it is the container itself, so the URL connects to nothing and
    the caller reports the service as down.

    That is what `from_container` is for, and this is what stops the two being
    confused again. Checked here as well as in test_deploy.py so a plugin is
    held to it too -- both its own units and what it contributes to a core
    service, which is where docs/plugins.md actually shows an author writing an
    address. A household service dialing 127.0.0.1 from its own container fails
    exactly the same way.
    """
    problems = []
    for name, spec in services.items():
        # The same filter the rest of cmd_check_contract applies. A service the
        # household switched off has no containers, so holding its units to a
        # rule about containers only blocks a deploy over a unit that will
        # never exist.
        if name != "admin" and not (
                (cfg.get("services") or {}).get(name) or {}).get("enabled", True):
            continue
        for unit in spec.get("units", []) or []:
            where = f"{name}/{unit['name']}"
            parsed: dict = {}
            for filename in compose_files_for(unit, cfg):
                path = unit_dir(unit) / filename
                if not path.exists():
                    continue
                try:
                    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError as exc:
                    # Reported, not skipped: a file this cannot read is the one
                    # most likely to be wrong, and a silent `continue` here is
                    # the `|| echo` of a health check.
                    problems.append(
                        f"{where}: {filename} is not valid YAML, so nothing "
                        f"here could be checked ({exc.__class__.__name__})")
                    continue
                for svc_name, svc in (doc.get("services") or {}).items():
                    if isinstance(svc, dict):
                        parsed.setdefault(svc_name, {}).update(svc)

            try:
                env = interpolate(unit.get("env") or {}, cfg)
            except DeployError as exc:
                problems.append(f"{where}: {str(exc).splitlines()[0]}")
                continue

            # What a plugin adds to this service. It is the one place
            # docs/plugins.md actually shows an author writing an address, and
            # it reaches the container through `env_file:` rather than as
            # `${VAR}` -- so without both halves of this a contributed
            # `http://127.0.0.1:8095` would sail through the check that exists
            # for it.
            contributed: dict = {}
            if manifest is not None:
                try:
                    contributed = (plugin_contributions(manifest, name, cfg)
                                   .get("env") or {})
                except DeployError as exc:
                    problems.append(f"{where}: {str(exc).splitlines()[0]}")
            contributions_file = unit.get("contributions_env_file")

            # Variables that are the service's *own* address rather than
            # something it dials. Loopback is right for those on a one-PC
            # install -- they end up in links a person clicks from that
            # machine -- and the gateway name would be wrong. Declared with a
            # reason, like `status_only:` on a verify check -- and, like it,
            # checked, so a rename cannot leave an exemption standing over a
            # variable that no longer exists.
            self_urls = unit.get("self_urls") or {}
            for key in sorted(self_urls):
                if key not in env:
                    problems.append(
                        f"{where}: self_urls exempts {key}, which this unit's "
                        f"env: does not set")

            # Per service, not per unit. Network mode, the aliases in
            # `extra_hosts:` and the variables read are all attributes of one
            # compose service: a host-networked sidecar does not make its
            # bridge-networked neighbours safe, and an alias declared on the
            # database does not help the container doing the dialling.
            for svc_name, svc in sorted(parsed.items()):
                wanted = compose_service_variables(svc)
                host_net = svc.get("network_mode") == "host"
                aliases = compose_host_aliases(svc)
                delivered = dict(env)
                if contributed and contributions_file in compose_env_files(svc):
                    delivered.update(contributed)
                    wanted |= set(contributed)
                for key, value in sorted(delivered.items()):
                    if not isinstance(value, str) or key not in wanted:
                        continue
                    if host_net:
                        # Loopback is the host here, and right. The gateway
                        # name is the error: a host-networked container has no
                        # docker DNS, so it resolves nowhere.
                        if CONTAINER_GATEWAY in value:
                            problems.append(
                                f"{where}: {svc_name} is on host networking and "
                                f"{key} is {value}; {CONTAINER_GATEWAY} does not "
                                f"resolve there. Use {{hosts.<role>.address}}")
                        continue
                    if key not in self_urls and is_loopback_address(value):
                        problems.append(
                            f"{where}: {key} is {value}, which inside {svc_name} "
                            f"is {svc_name}. Use {{hosts.<role>.from_container}}")
                    if CONTAINER_GATEWAY in value and CONTAINER_GATEWAY not in aliases:
                        problems.append(
                            f"{where}: {svc_name} is given {key}={value} but "
                            f"declares no `extra_hosts: {CONTAINER_GATEWAY}:"
                            f"host-gateway`")
    return problems


def manifest_invariants(services: dict) -> list[str]:
    """Everything a manifest can get wrong without any config or secrets.

    Split out of the deployer's own test so that `--check-contract` can run it
    over the *merged* set. The tests read deploy/manifest.yml from disk, which
    means a plugin's units would have escaped every one of these -- and R2's
    whole promise is that a plugin service is indistinguishable from a shipped
    one downstream.

    Returns problems; an empty list is a pass.
    """
    problems = []
    projects: dict[str, str] = {}

    for svc, spec in services.items():
        # `state:` belongs to a unit. Declared one level up the deployer
        # silently ignores it -- no directory created, no guard_state_paths(),
        # and whatever the compose file falls back to is where it writes.
        if "state" in spec:
            problems.append(f"{svc}: declares state outside a unit")

        for unit in spec.get("units", []) or []:
            where = f"{svc}/{unit.get('name', '?')}"

            entries = unit.get("compose", "docker-compose.yml")
            if isinstance(entries, str):
                entries = [entries]
            files = []
            for entry in entries:
                filename = entry if isinstance(entry, str) else entry.get("file")
                if not filename or not (unit_dir(unit) / filename).exists():
                    problems.append(f"{where}: no compose file {filename}")
                    continue
                if isinstance(entry, dict) and not entry.get("when"):
                    problems.append(f"{where}: overlay {filename} has no `when:`")
                files.append(filename)

            # Two units sharing a Compose project means `up --remove-orphans`
            # in one deletes the other's containers -- green, both times.
            project = unit.get("compose_project") or f"{svc}-{unit.get('name')}"
            if project in projects:
                problems.append(
                    f"{where} and {projects[project]} share compose project {project}")
            projects[project] = where

            text = ""
            for filename in files:
                text += (unit_dir(unit) / filename).read_text(encoding="utf-8")

            # A unit whose services are *rendered* has half its compose file in
            # a Python module, and every check below reads only the declared
            # ones. The per-member assistants are the whole of that: they mount
            # `${CODE_WORKSPACE_DIR}` and nothing in `compose:` mentions it, so
            # moving the code broker into its own unit made a correct `state:`
            # entry look decorative. Rendering it here is not an option --
            # this function takes no config on purpose, so that a plugin's units
            # face the same checks with nothing configured -- but the renderer's
            # own source carries the variable names as literals, which is the
            # thing being asked about. Read them from there.
            gen = unit.get("generated_compose")
            if gen and gen in GENERATORS:
                module = Path(__file__).resolve().parent / f"{GENERATORS[gen]}.py"
                if module.exists():
                    text += module.read_text(encoding="utf-8")

            # A `state:` entry names a variable so a compose file can mount it.
            # When nothing reads that name the declaration is decorative: the
            # deployer creates, guards and documents one directory while the
            # container writes to another.
            for state in unit.get("state", []) or []:
                var = state.get("env")
                if var and "${" + var not in text:
                    problems.append(
                        f"{where}: state {state.get('path')} is exported as "
                        f"{var}, which no compose file reads")

            # An `env:` name nothing on the other side reads. The deployer
            # exports it, no compose file lists it, and no script in the unit
            # mentions it -- so it reaches nothing, and the setting it came
            # from does nothing. Thirty of these had accumulated, and one of
            # them mattered: the portal reads WHISPER_URL and never received
            # it, so transcription fell back to `whisper.home`, a name
            # this stack does not resolve.
            #
            # A name may be read by a `pre:` script rather than by a container,
            # which is why the whole unit directory counts and not just the
            # compose files.
            # A generated compose file (the per-member blocks) is git-ignored
            # and written at deploy time, so a fresh checkout has none on disk
            # and every key it reads looked unread. What it *would* say is
            # rendered here, from the shipped example config.
            tree = generated_compose_text(unit)
            for path in sorted(unit_dir(unit).rglob("*")):
                if path.is_file() and path.suffix in {
                        ".yml", ".yaml", ".py", ".sh", ".js", ".php", ".json", ""}:
                    try:
                        tree += path.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        pass
            for name in sorted(unit.get("env") or {}):
                if name not in tree:
                    problems.append(
                        f"{where}: exports {name}, which nothing in "
                        f"{unit['dir']} reads")

            for check in unit.get("verify", []) or []:
                # A check is run from inside the pushed unit directory, which
                # holds the *contents* of `dir:`.
                script = check.get("script")
                if script and not (unit_dir(unit) / script).exists():
                    problems.append(f"{where}: verify script {script} does not exist")
                # A bare status code passes against a redirect to a login page
                # and against a 404 handler that returns 200.
                if "http" in check and not (
                        {"expect_json", "expect", "expect_status", "status_only"}
                        & set(check)):
                    problems.append(
                        f"{where}: {check['http']} asserts nothing a request can fail")
    return problems


def cmd_check_contract(cfg: dict, manifest: dict, secrets: dict) -> int:
    """Assert that what the deployer exports covers what compose interpolates.

    This exists because it did not, and nothing noticed: `docker compose
    config -q` exits 0 on an unset variable, so a bind mount silently pointed
    at a relative path inside the tree the deployer rsyncs with --delete. That
    is the shape of four separate outages in the stack this came from.
    """
    problems = 0
    checked = 0

    # The admin page holds the docker socket, the secrets file and a Deploy
    # button, and the proxies answer from outside the house. Nothing may route
    # one to the other. Checked here so it is asserted on demand and before a
    # deploy, rather than being a rule people remember.
    for problem in admin_is_not_proxied(cfg):
        out.fail(problem)
        problems += 1

    # all_services, not manifest["services"]: the optional cloud proxy was
    # never contract-checked, and its missing SESSION_SIGNING_KEY mapping
    # shipped a container that raises at import. Built once -- it re-runs the
    # plugin name-collision merge every time it is called.
    services = all_services(manifest, cfg)

    # The config-free invariants, over the merged set so a plugin's units are
    # held to them too.
    for problem in manifest_invariants(services):
        print(f"  {problem}")
        problems += 1

    for problem in loopback_reachability_problems(services, cfg, manifest):
        print(f"  {problem}")
        problems += 1

    for name, spec in services.items():
        if name != "admin" and not ((cfg.get("services") or {}).get(name) or {}).get("enabled", True):
            continue
        for unit in spec.get("units", []):
            files = compose_files_for(unit, cfg)
            missing_files = [f for f in files if not (unit_dir(unit) / f).exists()]
            if missing_files:
                print(f"  {name}/{unit['name']}: no {', '.join(missing_files)}")
                problems += 1
                continue
            compose = unit_dir(unit) / files[0]
            checked += 1
            try:
                env = collect_env(spec, unit, secrets, cfg,
                                  plugin_contributions(manifest, name, cfg))
            except DeployError as exc:
                print(f"  {name}/{unit['name']}: {exc}".split("\n")[0])
                problems += 1
                continue

            declared = spec.get("secrets", {}) or {}
            members = member_ids(cfg)
            optional = set(declared.get("optional", [])) | set(
                expand_per_member(declared.get("per_member", []), members))

            wanted = {}
            for f in files:
                for var, kind in compose_variables(unit_dir(unit) / f).items():
                    if wanted.get(var) != "bare":
                        wanted[var] = kind
            # A key the service declares optional is deliberately unset when its
            # feature is off; that is a decision, not an omission. Anything else
            # unguarded is a variable nothing in the package knows about.
            unsupplied = [v for v, kind in sorted(wanted.items())
                          if v not in env and kind == "bare" and v not in optional]
            if unsupplied:
                print(f"  {name}/{unit['name']}: {len(unsupplied)} unguarded "
                      f"variable(s) nothing supplies")
                for var in unsupplied:
                    print(f"      {var}")
                problems += len(unsupplied)

            # A relative bind mount resolves inside the directory push() sends
            # with --delete, so the container's state is destroyed by the next
            # deploy. Declared state paths are absolute and guarded elsewhere;
            # this catches the ones the manifest never declared.
            try:
                doc = yaml.safe_load(compose.read_text()) or {}
            except yaml.YAMLError:
                doc = {}
            for svc_name, svc in (doc.get("services") or {}).items():
                for vol in svc.get("volumes") or []:
                    if not isinstance(vol, str) or ":" not in vol:
                        continue
                    parts = vol.split(":")
                    src, mode = parts[0], (parts[2] if len(parts) > 2 else "")
                    shipped = (compose.parent / src).exists() if src.startswith(".") else False
                    if src.startswith(("./", "../")):
                        # A read-only mount of a file that ships with the service
                        # is configuration, not state: the deploy is supposed to
                        # replace it. Everything else relative is state that a
                        # re-deploy would destroy.
                        if mode == "ro" and shipped:
                            continue
                        print(f"  {name}/{unit['name']}: {svc_name} mounts {src} "
                              f"- relative, so a re-deploy destroys it")
                        problems += 1
                    elif src.startswith("${"):
                        var = COMPOSE_VAR.match(src)
                        if var and var.group(1) not in env:
                            dflt = (var.group(2) or "")[2:]
                            if dflt.startswith("."):
                                print(f"  {name}/{unit['name']}: {svc_name} mounts "
                                      f"{src} - unset, falls back inside the deploy tree")
                                problems += 1

            # A `state:` entry names a variable so the compose file can mount
            # it. When the compose file never reads that name, the declaration
            # is decorative: the deployer creates and guards one directory
            # while the container writes somewhere else entirely. That is what
            # `~/nanobot-users` was -- the real state sat outside `paths.state`,
            # so `guard_state_paths()` was inspecting a path nothing used,
            # `docs/backups.md` documented empty directories, and the init
            # container chowned a tree no container ever mounted.
            for entry in unit.get("state", []) or []:
                var = entry.get("env")
                if var and var not in wanted:
                    print(f"  {name}/{unit['name']}: state {entry['path']} is "
                          f"exported as {var}, which no compose file reads")
                    problems += 1

            # An external network nothing creates fails `up` on a fresh host,
            # and `config -q` does not check for it.
            for net_name, net in (doc.get("networks") or {}).items():
                if isinstance(net, dict) and net.get("external"):
                    if not any(u.get("pre_create_networks") for u in spec.get("units", [])):
                        print(f"  {name}/{unit['name']}: network {net_name!r} is "
                              f"external and nothing creates it")
                        problems += 1

    if problems:
        print(f"\n{problems} contract problem(s) across {checked} unit(s)")
        return 1
    print(f"contract ok: {checked} unit(s), every compose variable supplied")
    return 0


def cmd_list(cfg: dict, manifest: dict) -> int:
    print(f"{'service':<16} {'role':<9} {'state':<9} description")
    print("-" * 78)
    for name, spec in manifest["services"].items():
        svc = (cfg.get("services") or {}).get(name) or {}
        enabled = name == "admin" or svc.get("enabled", True)
        role = spec.get("role", svc.get("host", "?"))
        state = "always" if name == "admin" else ("enabled" if enabled else "off")
        print(f"{name:<16} {role:<9} {state:<9} {spec.get('description', '')}")

    vps = cfg.get("cloud", {}).get("vps", {})
    if manifest.get("optional_services"):
        print()
        if vps.get("enabled") and not vps.get("managed", True):
            print("optional (a VPS is configured, and not deployed from here "
                  "-- cloud.vps.managed):")
        else:
            print("optional (off unless cloud.vps.enabled):")
        for name, spec in manifest["optional_services"].items():
            state = "enabled" if vps.get("enabled") else "off"
            print(f"{name:<16} {'vps':<9} {state:<9} {spec.get('description', '')}")

    # Named as plugins rather than folded in with the rest: which services this
    # package ships and which the household brought is the first thing somebody
    # reading this list needs to know.
    for plugin in manifest.get("_plugins") or []:
        services = (plugin["doc"].get("services") or {})
        if not services:
            continue
        print()
        print(f"plugin {plugin['name']} ({plugin['root']}):")
        for name, spec in services.items():
            svc = (cfg.get("services") or {}).get(name) or {}
            state = "enabled" if svc.get("enabled", True) else "off"
            role = spec.get("role", "?")
            print(f"{name:<16} {role:<9} {state:<9} {spec.get('description', '')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy the home stack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1],
    )
    parser.add_argument("services", nargs="*",
                        help="service names, or 'all', or 'list'")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen; change nothing")
    parser.add_argument("--only", metavar="UNIT",
                        help="deploy a single unit of a single service")
    parser.add_argument("--remote-root", default=None,
                        help="where services live on the targets "
                             "(default: ~/.local/share/home-stack)")
    parser.add_argument("--check-contract", action="store_true",
                        help="assert every compose variable is supplied; "
                             "change nothing")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        out.color = False

    if not args.services:
        parser.print_help()
        return 2

    try:
        # Said before anything is read out of it. Preferring the deployed copy
        # is right, and it makes an edit to the checkout do nothing -- so an
        # edit that will be ignored has to announce itself rather than be
        # discovered later by its absence.
        divergence = config_divergence()
        if divergence:
            out.warn(divergence)

        cfg = load_yaml(CONFIG)
        apply_service_renames(cfg)
        add_new_services(cfg)
        apply_config_defaults(cfg)
        warn_admin_exposure(cfg, args.services)
        manifest = load_yaml(MANIFEST)
        manifest["_plugins"] = load_plugins(cfg)

        if args.services == ["list"]:
            return cmd_list(cfg, manifest)

        # Before anything is deployed, because a service brought up against a
        # path whose contents have not arrived yet writes a fresh empty state
        # and the old one is orphaned -- which is the failure this exists for.
        #
        # `dry_run` reaches it, because `--dry-run` and `plan` promise to change
        # nothing and this is the most changing thing in the package. It still
        # *checks*: a plan that said nothing about an 881 MB move waiting to
        # happen would be a plan missing the only step that matters.
        for line in migrate_paths(cfg, dry_run=args.dry_run):
            out.ok(line)

        secrets = load_secrets(SECRETS)
        # Derived values the manifest reads as {derived.*}. After the secrets,
        # because the Ollama key is one of them.
        cfg = derive(cfg, secrets)
        # Before the contract check and before any deploy: a name pointing at
        # the wrong machine passes every other check this deployer has.
        check_dns_points_at_its_service(cfg, manifest)

        if args.check_contract:
            return cmd_check_contract(cfg, manifest, secrets)

        targets = build_targets(cfg, args.dry_run)
        warn_vps_is_elsewhere(cfg, targets)
        note_vps_unmanaged(cfg)

        # Owned by the deploying user, so the common case -- one machine,
        # everything on it -- needs no sudo and no ssh at all. /opt/home-stack
        # was the old default and required root to create even once, which made
        # the very first documented deploy fail on a fresh install.
        # HOME_STACK_DEPLOY_ROOT before `$HOME`, because the admin container
        # runs this and its `$HOME` is /root while the host's is somebody
        # else's. The two have to be the same string: the container mounts this
        # path at the same path inside as out, writes a service's tree into it,
        # and compose then resolves that tree's bind mounts against the host.
        remote_root = args.remote_root or os.environ.get(
            "HOME_STACK_DEPLOY_ROOT") or os.path.expanduser(
            "~/.local/share/home-stack")

        available = all_services(manifest, cfg)
        manifest = {**manifest, "services": available}

        if args.services == ["all"]:
            names = [
                n for n, s in available.items()
                if n == "admin"
                or ((cfg.get("services") or {}).get(n) or {}).get("enabled", True)
            ]
        else:
            names = args.services
            unknown = [n for n in names if n not in available]
            if unknown:
                raise DeployError(
                    f"unknown service(s): {', '.join(unknown)}. "
                    f"Try: ./home-stack list"
                )

        if args.only and len(names) != 1:
            raise DeployError("--only takes a single service")

        names = order_services(names, manifest)

        out.step(f"deploying {len(names)} service(s): {', '.join(names)}")
        if args.dry_run:
            out.warn("dry run - nothing will be changed")
        print()

        failed: list[tuple[str, str]] = []
        for name in names:
            blockers = [
                dep for dep in manifest["services"].get(name, {}).get("depends_on", [])
                if dep in {f for f, _ in failed}
            ]
            if blockers:
                # Ordering alone is not a dependency: deploying onto a service
                # that just failed produces a second, confusing failure that
                # hides the first.
                reason = f"skipped: depends on {', '.join(blockers)}, which failed"
                out.step(f"{name}")
                out.fail(reason)
                failed.append((name, reason))
                print()
                continue
            started = time.time()
            try:
                deploy_service(name, manifest["services"][name], cfg, secrets,
                               targets, args.only, remote_root,
                               manifest.get("_plugins"))
                out.ok(f"{name} done in {time.time() - started:.1f}s")
            except DeployError as exc:
                out.fail(str(exc))
                failed.append((name, str(exc)))
            print()

        if failed:
            out.fail(f"{len(failed)} of {len(names)} service(s) failed:")
            for name, reason in failed:
                print(f"      {name}: {reason.splitlines()[0]}", file=sys.stderr)
            return 1

        out.ok(f"all {len(names)} service(s) deployed and answering")
        return 0

    except DeployError as exc:
        out.fail(str(exc))
        return 1
    except KeyboardInterrupt:
        out.fail("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
