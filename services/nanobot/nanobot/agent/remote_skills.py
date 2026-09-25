"""Skills served by the service they describe.

The problem
-----------
A skill like `home-lights` is a description of somebody else's HTTP API. It
shipped inside this package, so the description and the API lived in two repos
behind two deploy jobs, and the gap between them was a real outage shape rather
than a theoretical one: add an endpoint to the light service and Alfred cannot
use it until nanobot is rebuilt and all six instances are rolled; change one and
Alfred keeps confidently calling the old contract until somebody notices.

The fix is to let the service hand out its own skill. Anything answering

    GET <its API base>/skill

with the envelope below owns the instructions for itself, and editing them is a
deploy of that one small service — or, if it reads the text from a mounted
config directory, no deploy at all.

The envelope
------------
    {
      "name":         "home-lights",       # informational; see below
      "version":      "3f9c…",             # any string that changes with content
      "mode":         "replace" | "append",
      "description":  "…",                 # optional frontmatter override
      "instructions": "<SKILL.md, frontmatter optional>",
      "python":       "<SKILL_PYTHON.md source>"   # optional
    }

`replace` (the default) means *this is the skill*; the copy bundled in this
package stays only as the floor for when the service is unreachable. `append`
means the bundled skill stands and this is extra: the house's own room names,
seasonal wording, whatever is true this month but not worth a release.

The skill is filed under the name we *asked* for, not the `name` the service
answers with. Discovery is keyed by environment variable (below), so the
requester already knows which skill it is asking about, and letting a response
rename itself would let one service quietly shadow another's skill.

How it reaches the prompt
-------------------------
Fetched content is written to `<workspace>/.skills-remote/<name>/SKILL.md` (plus
`SKILL_PYTHON.md`), and `SkillsLoader` reads that directory as a third root
between the workspace and the builtins. Everything downstream — the summary
line, the `path` the invocation interceptor maps a `{"skill": …}` block back to,
`_load_skill_python_guide`, the model reading the file itself — keeps working on
files, unchanged, because it is still looking at files.

That choice also decides the failure mode, which is the important half. The
refresh runs on a background thread, so a service that has gone slow costs the
turn nothing; a service that has gone *away* costs it nothing either, because
last week's materialised copy is still on disk. Only an explicit 404 — the
service saying "I have no skill", not failing to say anything — removes it, or
no row naming the skill at all, which means nobody is asking any more.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import ipaddress
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# Which environment variable names which service's API, and which skill that
# service is the authority on.
#
# Every entry here is a variable the instances already set, and the skills'
# own generated code already reads (see each SKILL_PYTHON.md). A third value
# rewrites the path when several skills live behind one base URL — HomeCore
# serves tasks, grocery, menu, geo and family-message off one host, and the
# convention for reaching them from the tasks URL is a path swap, spelled out
# in agent/tools/shell.py's note on _SKILL_OWNED_SIGNALS.
#
# A service opts in by answering /skill. It opts out by not answering, which is
# what every service here does today until it grows the endpoint — so adding a
# row costs nothing and changes nothing until the other side is ready.
SERVICE_SKILL_ENV: tuple[tuple[str, str, tuple[str, str] | None], ...] = (
    ("lights",         "HOME_LIGHTS_API_URL",   None),
    ("paperless",      "PAPERLESS_URL",         None),
    ("n8n",            "NANOBOT_N8N_BASE_URL",  None),
    ("finance",        "FINANCE_API_URL",       None),
    ("tasks",          "TASKS_API_URL",         None),
    ("grocery",        "TASKS_API_URL",         ("/tasks/api", "/grocery/api")),
    ("menu",           "TASKS_API_URL",         ("/tasks/api", "/menu/api")),
    ("geo",            "TASKS_API_URL",         ("/tasks/api", "/geo/api")),
    ("file-share",     "TASKS_API_URL",         ("/tasks/api", "/files/api")),
    ("family-message", "TASKS_API_URL",         ("/tasks/api", "/chat/api")),
    # The code broker serves its own, for the reason at the top of this file:
    # it is an API in this repo but a *different* deployable, and the skill has
    # to be able to change with the verbs rather than with the agent.
    ("code",           "CODE_BROKER_URL",       None),
    # The wall's API. Supplied by the deployer from `dns.cameras` and
    # `services.home-cameras.web_port`; camera-feed's own code used to carry a
    # host and the port *inside* that container instead.
    ("camera-feed",    "CAMERA_API_URL",        None),
)

# Every name this package owns. A supplied row may not claim any of them.
#
# Both halves matter. `SERVICE_SKILL_ENV` is checked because a shipped row whose
# variable happens to be unset must still hold its name -- on `nanobot-house`
# TASKS_API_URL and PAPERLESS_URL are deliberately absent, which is exactly
# where an unguarded claim would land. The skills directory is checked because
# it ships 27 skills and the table above names 11 of them: `notifications`,
# `memory`, `document`, `whatsapp` and the rest were defended by nothing.
_BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


# A plugin's floor copy lives in the builtin directory too, and it must not be
# read as a skill this package ships. The deployer stages it there on purpose --
# SkillsLoader reads workspace, then remote, then builtin, so builtin is the
# only tier where the live copy the plugin's own service serves still wins --
# and then writes this marker beside it.
#
# Without the marker the feature refused itself: the floor made the remote entry
# a name collision, the entry was dropped, and the floor became the only copy.
# `backups=BACKUPS_API_URL` was ignored on every instance for exactly this
# reason, so the assistants ran a frozen snapshot of a skill whose service was
# sitting right there answering.
#
# Safe because the deployer has already refused any floor whose name collides
# with a shipped skill, twice: once against the plugin's declared skill name and
# once against the staging directory itself.
_PLUGIN_FLOOR_MARKER = ".plugin-floor"


def _shipped_skill_names() -> frozenset[str]:
    names = {skill for skill, _, _ in SERVICE_SKILL_ENV}
    try:
        names |= {d.name for d in _BUILTIN_SKILLS_DIR.iterdir()
                  if (d / "SKILL.md").is_file()
                  and not (d / _PLUGIN_FLOOR_MARKER).exists()}
    except OSError:
        pass
    return frozenset(names)


# A skill name is a directory name under the cache, and nothing else. Written
# as an allowlist because the sinks -- `cache_dir(workspace) / name` in
# `materialize` and `prune` -- give an unconstrained string the run of the
# filesystem: `../skills/file-share` lands in the workspace root, which outranks
# both the cache and the builtin copy, and an absolute name discards the prefix
# entirely. What gets written there is not inert -- `SKILL_PYTHON.md` is pulled
# apart by the runner and executed with the exec tool's credentials.
_SAFE_SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*\Z", re.I)


def _is_safe_skill_name(name: str) -> bool:
    return bool(_SAFE_SKILL_NAME.fullmatch(name)) and name not in (".", "..")


CACHE_DIRNAME = ".skills-remote"

# Bounds, all deliberately small. This runs on somebody's home LAN against
# services on the same switch; anything that needs longer than this is down.
_TIMEOUT_S = 3.0
_MAX_BYTES = 256 * 1024
_DEFAULT_TTL_S = 300.0

# The version last written, per (workspace, skill), so an unchanged answer does
# not rewrite files every five minutes and churn their mtimes.
_written: dict[tuple[str, str], str] = {}
_lock = threading.Lock()
_syncers: dict[str, "RemoteSkillSync"] = {}


def _enabled() -> bool:
    return (os.environ.get("NANOBOT_REMOTE_SKILLS") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _ttl_seconds() -> float:
    try:
        return max(30.0, float(os.environ.get("NANOBOT_REMOTE_SKILL_TTL") or _DEFAULT_TTL_S))
    except ValueError:
        return _DEFAULT_TTL_S


# Rows this image does not ship, named at run time. A household's own service
# cannot be listed above -- the core does not know it exists -- so the deployer
# passes its rows in, one per plugin that declares a skill.
#
#   NANOBOT_SKILL_SERVICES="switches=SWITCHES_API_URL,solar=SOLAR_URL:/api>/skill-api"
#
# Same shape as a row above: skill name, the variable naming the service, and
# an optional `old>new` path rewrite. Malformed entries are dropped with a
# warning rather than taking the agent down -- this is read at startup, and a
# typo in one plugin must not stop the assistant answering about anything else.
EXTRA_SKILL_SERVICES_VAR = "NANOBOT_SKILL_SERVICES"


def _extra_skill_rows(env: dict[str, str]) -> tuple[tuple[str, str, tuple[str, str] | None], ...]:
    rows = []
    shipped = _shipped_skill_names()
    for entry in (env.get(EXTRA_SKILL_SERVICES_VAR) or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, rest = entry.partition("=")
        var, _, swap = rest.partition(":")
        name, var = name.strip(), var.strip()
        if not name or not var:
            logger.warning("Ignoring malformed {} entry: {!r}",
                           EXTRA_SKILL_SERVICES_VAR, entry)
            continue
        if not _is_safe_skill_name(name):
            logger.warning(
                "Ignoring {} entry {!r}: {!r} is not a skill name. A name is a "
                "directory under the cache, so it may not contain a path.",
                EXTRA_SKILL_SERVICES_VAR, entry, name)
            continue
        if name in shipped:
            # Not `continue` at the `found` loop below: that only holds when the
            # shipped row resolved to a URL, so an unset variable handed the
            # name away. Refuse it here, where the answer does not depend on
            # which instance this is.
            logger.warning(
                "Ignoring {} entry {!r}: {!r} is a skill this package ships.",
                EXTRA_SKILL_SERVICES_VAR, entry, name)
            continue
        rewrite = None
        if swap:
            old_path, _, new_path = swap.partition(">")
            if not old_path or not new_path:
                logger.warning("Ignoring malformed path rewrite in {!r}", entry)
            else:
                rewrite = (old_path, new_path)
        rows.append((name, var, rewrite))
    return tuple(rows)


def discover_endpoints(env: dict[str, str] | None = None) -> dict[str, str]:
    """`{skill: url}` for every service this process has an address for.

    Nothing is probed here; a URL only means "there is a service at this
    address", never that it has a skill to give.
    """
    env = os.environ if env is None else env
    found: dict[str, str] = {}
    # Shipped rows first, so a plugin cannot quietly take over the name of a
    # skill this package owns: `found` is keyed by name and the built-in wins.
    for skill, var, swap in SERVICE_SKILL_ENV + _extra_skill_rows(env):
        if skill in found:
            continue
        base = (env.get(var) or "").strip()
        if not base:
            continue
        if swap:
            old, new = swap
            if old not in base:
                continue      # not the URL shape this rewrite was written for
            base = base.replace(old, new)
        base = base.rstrip("/")
        if not base.startswith(("http://", "https://")):
            continue
        found[skill] = f"{base}/skill"
    return found


class SkillGone(Exception):
    """The service answered, and answered that it has no skill (404)."""


# Hosts that are this house talking to itself. Named rather than pattern-matched
# at the call site because the decision below turns TLS verification off, and
# the list of addresses that deserve that is short and worth reading.
_HOUSE_SUFFIXES = ("host.docker.internal", ".home", ".local", ".internal")


def _configured_house_hosts() -> frozenset[str]:
    """Hosts the deployer itself told this container are the household's.

    The suffix list above cannot be the whole answer. `dns:` is a setting and a
    household picks the suffix -- the manifest now builds TASKS_API_URL,
    PAPERLESS_URL and HOMECORE_CONTAINER_URL out of those names, so a house
    whose domain is anything but `.home`, `.local` or `.internal` had every one
    of its own skills fail `CERTIFICATE_VERIFY_FAILED` against the certificate
    the local CA issued for it. That is the exact failure `_ssl_context_for`
    was written for, arriving by a name instead of by an address.

    Read from the same variables `discover_endpoints` builds the skill table
    from, so a host is "the house" here for precisely the reason it is a
    household service at all -- rather than because somebody guessed a suffix.
    """
    hosts = set()
    for _skill, var, _swap in SERVICE_SKILL_ENV:
        url = (os.environ.get(var) or "").strip()
        if not url:
            continue
        host = urllib.parse.urlparse(url).hostname
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


def _is_house(host: str) -> bool:
    """Whether *host* is an address on this household's own network."""
    host = (host or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(_HOUSE_SUFFIXES):
        return True
    if host in _configured_house_hosts():
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def _ssl_context_for(url: str) -> ssl.SSLContext | None:
    """An unverified context for the house's own services, None for anything else.

    HomeCore serves its own self-signed certificate on the home network, and
    every household skill is fetched from it -- `/tasks/api/skill`,
    `/grocery/api/skill`, `/menu/api/skill`, `/geo/api/skill`,
    `/files/api/skill`, `/chat/api/skill`. Verifying it fails, so *every one of
    them* came back `CERTIFICATE_VERIFY_FAILED` on every refresh, every five
    minutes, at DEBUG. The agent was left without the household's own skills and
    reached for a `direct-curl-execution` workaround -- a skill whose stated
    purpose was "bypassing skill import errors" -- which then hit the exec
    guard for internal URLs. Two layers of symptom over one unverifiable cert.
    Every other caller in this codebase already passes verify=False for exactly
    this; this one did not.

    Scoped to private addresses rather than switched off outright, because
    `fetch` takes whatever URL a service declares and a skill served from the
    public internet should still have to prove who it is. The self-signed
    certificate is a fact about the house, so the exemption is too.
    """
    if urllib.parse.urlparse(url).scheme != "https":
        return None
    if not _is_house(urllib.parse.urlparse(url).hostname or ""):
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


# The envelope's own keys, for telling "tried and got it wrong" from "does not
# implement this endpoint". `instructions` is deliberately not among them: its
# absence is the thing being diagnosed, so requiring it would make the test
# vacuous.
_ENVELOPE_KEYS = frozenset({"name", "version", "mode", "description", "python"})


def fetch(url: str, *, timeout: float = _TIMEOUT_S) -> dict[str, Any] | None:
    """The envelope at *url*, or None if it could not be had.

    Raises SkillGone on a 404, which is the one answer that means something
    other than "try again later" — see the note about pruning at the top.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=_ssl_context_for(url)) as response:
            raw = response.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SkillGone(url) from e
        logger.debug("Remote skill {} answered {}", url, e.code)
        return None
    except Exception as e:                      # timeout, DNS, refused, TLS…
        logger.debug("Remote skill {} unreachable: {}", url, e)
        return None

    if len(raw) > _MAX_BYTES:
        logger.warning("Remote skill {} is over {} bytes; ignored", url, _MAX_BYTES)
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        # Debug, not a warning: most services answer *something* at an unknown
        # path — a login page, a SPA shell, a framework 200 — and every one of
        # them would otherwise log a warning every refresh, forever, for the
        # entirely normal state of not implementing this endpoint.
        logger.debug("Remote skill {} did not answer JSON: {}", url, e)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("instructions"), str):
        # Same reasoning as the JSON check above, which stopped one case short.
        # A service that does not implement this endpoint may still answer JSON
        # at it -- paperless returns its *login form* as a well-formed
        # `{"form": ..., "html": ...}` when asked with `Accept:
        # application/json`, 200 and all. That is not a malformed envelope, it
        # is a polite 'no', and warning about it every refresh forever is the
        # noise the branch above exists to prevent.
        #
        # So the warning is kept for the case it was written for: something
        # that is *trying* to be an envelope and got it wrong, which is a
        # household's own service with a bug worth seeing. Anything else is
        # debug.
        if isinstance(payload, dict) and _ENVELOPE_KEYS & payload.keys():
            logger.warning("Remote skill {} has no instructions", url)
        else:
            logger.debug("Remote skill {} is not a skill endpoint", url)
        return None
    return payload


# ── composing the document ───────────────────────────────────────────────────

def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """(parsed frontmatter, body). Frontmatter is optional in a served skill."""
    if not text.startswith("---"):
        return None, text
    rest = text[3:].lstrip("\r\n")
    end = rest.find("\n---")
    if end == -1:
        return None, text
    head, body = rest[:end], rest[end + 4:].lstrip("\r\n")
    try:
        parsed = yaml.safe_load(head)
    except yaml.YAMLError:
        return None, text
    return (parsed, body) if isinstance(parsed, dict) else (None, text)


def _render(front: dict[str, Any], body: str) -> str:
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{head}---\n\n{body.strip()}\n"


def compose(name: str, envelope: dict[str, Any], bundled: str | None) -> str:
    """The SKILL.md to write for *name*, given what the service sent.

    *bundled* is the copy shipped in this package, or None if there isn't one —
    a service is allowed to introduce a skill nanobot has never heard of, which
    is the whole point of the endpoint being a contract rather than a patch.
    """
    remote_front, remote_body = _split_frontmatter(envelope["instructions"])
    bundled_front, bundled_body = _split_frontmatter(bundled or "")
    mode = (envelope.get("mode") or "replace").strip().lower()

    if mode == "append" and bundled:
        front = dict(bundled_front or {})
        body = f"{bundled_body.strip()}\n\n---\n\n{remote_body.strip()}"
    else:
        front = dict(remote_front or bundled_front or {})
        body = remote_body

    front.setdefault("name", name)
    # An explicit `description` in the envelope is the override: it is the one
    # line the model reads about every skill on every turn, before deciding
    # whether to open it, so being able to change it without a release is worth
    # more than the rest of the frontmatter put together.
    if isinstance(envelope.get("description"), str) and envelope["description"].strip():
        front["description"] = envelope["description"].strip()
    front.setdefault("description", name)
    if isinstance(envelope.get("metadata"), (dict, str)):
        front["metadata"] = envelope["metadata"]
    return _render(front, body)


# ── materialising ────────────────────────────────────────────────────────────

def cache_dir(workspace: Path) -> Path:
    return Path(workspace).expanduser() / CACHE_DIRNAME


def _cache_target(workspace: Path, name: str) -> Path | None:
    """The directory *name* is cached in, or None if it is not one.

    `_extra_skill_rows` already refuses a name that is not a bare directory, so
    this is the second of two checks rather than the only one. It is here
    because these two functions are what actually write and unlink, and a
    reader of `materialize` should not have to go and find the parser to know
    the path is bounded.
    """
    if not _is_safe_skill_name(name):
        logger.warning("Refusing remote skill {!r}: not a skill name", name)
        return None
    root = cache_dir(workspace)
    target = root / name
    if target.resolve().parent != root.resolve():
        logger.warning("Refusing remote skill {!r}: resolves outside {}",
                       name, root)
        return None
    return target


def _write(path: Path, text: str) -> None:
    """Write *text* to *path* without ever leaving a half-written skill behind.

    The reader is a different process's prompt build, running at any moment; a
    truncated SKILL.md would reach a model as a truncated instruction rather
    than as an error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def materialize(workspace: Path, name: str, envelope: dict[str, Any],
                bundled: str | None) -> bool:
    """Write the served skill into the cache. True when anything changed."""
    key = (str(workspace), name)
    version = str(envelope.get("version") or "")
    target = _cache_target(workspace, name)
    if target is None:
        return False
    with _lock:
        unchanged = version and _written.get(key) == version and (target / "SKILL.md").is_file()
    if unchanged:
        return False

    _write(target / "SKILL.md", compose(name, envelope, bundled))
    python = envelope.get("python")
    python_path = target / "SKILL_PYTHON.md"
    if isinstance(python, str) and python.strip():
        _write(python_path, python)
    elif python_path.exists():
        # The service dropped its translation guide. Leaving the old one behind
        # would keep executing code for an API that no longer describes itself
        # that way — worse than having none, which merely costs a round trip
        # through the LLM translator.
        python_path.unlink()

    with _lock:
        _written[key] = version
    logger.info("Remote skill '{}' updated from its service (version {})", name, version or "?")
    return True


def prune(workspace: Path, name: str, why: str = "withdrawn by its service") -> bool:
    """Drop the cached copy of *name*. True when something was removed."""
    target = _cache_target(workspace, name)
    if target is None or not target.is_dir():
        return False
    for child in target.iterdir():
        try:
            child.unlink()
        except OSError:
            pass
    try:
        target.rmdir()
    except OSError:
        return False
    with _lock:
        _written.pop((str(workspace), name), None)
    logger.info("Remote skill '{}' {}", name, why)
    return True


def _prune_unclaimed(workspace: Path, claimed: dict[str, str],
                     results: dict[str, str]) -> None:
    """Drop every cached skill that no row names any more.

    The 404 above is one way a skill stops existing; this is the other. Delete
    a row from the table, or drop the plugin that supplied one, and its service
    is never asked again -- so it can never answer 404, and the loader reads the
    cache directory, not the table. Deleting `home-lights`'s row, tried on
    2026-09-11, would have left it in every prompt for as long as the workspace
    lived, while the change read as done.

    Unreachable is not unclaimed: a row whose service is down still names the
    skill, and its last copy stays, exactly as above.
    """
    root = cache_dir(workspace)
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name not in claimed:
            if prune(workspace, entry.name, "dropped: no service is configured for it"):
                results[entry.name] = "gone"


def sync_once(workspace: Path, builtin_dir: Path,
              endpoints: dict[str, str] | None = None) -> dict[str, str]:
    """Refresh every discovered skill once. Returns `{skill: outcome}`.

    Never raises: this runs on a background thread whose death would silently
    freeze every skill at whatever it last fetched.
    """
    results: dict[str, str] = {}
    endpoints = discover_endpoints() if endpoints is None else endpoints
    for name, url in endpoints.items():
        try:
            envelope = fetch(url)
        except SkillGone:
            results[name] = "gone" if prune(workspace, name) else "absent"
            continue
        except Exception:
            logger.exception("Remote skill sync failed for {}", name)
            results[name] = "error"
            continue
        if envelope is None:
            results[name] = "unreachable"
            continue
        try:
            bundled_path = Path(builtin_dir) / name / "SKILL.md"
            bundled = bundled_path.read_text(encoding="utf-8") if bundled_path.is_file() else None
            results[name] = "updated" if materialize(workspace, name, envelope, bundled) else "unchanged"
        except Exception:
            logger.exception("Could not write remote skill {}", name)
            results[name] = "error"
    try:
        _prune_unclaimed(workspace, endpoints, results)
    except Exception:
        logger.exception("Could not prune unclaimed remote skills")
    return results


class RemoteSkillSync:
    """One background refresher per workspace.

    Started from `SkillsLoader.__init__` rather than from an entry point on
    purpose: nanobot is started as a CLI, an API server, a cron runner and a
    subagent, and a skill that only refreshes under one of them is a skill that
    is stale exactly where nobody is looking.
    """

    # How long a cold start may wait for the first fetch. Only ever paid once
    # per workspace, and bounded on purpose: a family instance lists half a
    # dozen services, most of which do not serve a skill, and three seconds of
    # timeout each would put twenty seconds between `docker start` and Alfred
    # answering anything. Whatever has not arrived by then arrives on the
    # refresh, and the bundled skills cover the gap.
    COLD_START_WAIT_S = 4.0

    def __init__(self, workspace: Path, builtin_dir: Path):
        self.workspace = Path(workspace)
        self.builtin_dir = Path(builtin_dir)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._first_pass = threading.Event()

    @classmethod
    def ensure_started(cls, workspace: Path, builtin_dir: Path) -> "RemoteSkillSync | None":
        """Start (once per workspace) the refresher for *workspace*."""
        if not _enabled() or not discover_endpoints():
            return None
        key = str(Path(workspace).expanduser())
        with _lock:
            existing = _syncers.get(key)
            if existing is not None:
                return existing
            syncer = cls(workspace, builtin_dir)
            _syncers[key] = syncer
        syncer.start()
        return syncer

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="remote-skills", daemon=True)
        self._thread.start()
        # Cold start only: with nothing on disk there is no served skill to use
        # while the first fetch runs, so the first turn would go out describing
        # an API by the bundled copy's rules. A warm cache returns immediately
        # and refreshes behind the turn.
        if not cache_dir(self.workspace).is_dir():
            self._first_pass.wait(self.COLD_START_WAIT_S)

    def _loop(self) -> None:
        while True:
            try:
                sync_once(self.workspace, self.builtin_dir)
            except Exception:
                logger.exception("Remote skill sync failed")
            self._first_pass.set()
            if self._stop.wait(_ttl_seconds()):
                return

    def stop(self) -> None:
        self._stop.set()


def reset_for_tests() -> None:
    """Forget every syncer and every remembered version."""
    with _lock:
        for syncer in _syncers.values():
            syncer.stop()
        _syncers.clear()
        _written.clear()
