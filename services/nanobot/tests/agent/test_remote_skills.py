"""Tests for nanobot.agent.remote_skills — skills served by their own service.

The failure this whole mechanism has to avoid is a *silent* one: a service that
is slow, down, or has stopped serving a skill must never leave the agent holding
half a document or none at all without saying so. Most of what is checked here
is therefore about what survives a bad answer, not about the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.agent import remote_skills as rs
from nanobot.agent.skills import SkillsLoader


@pytest.fixture(autouse=True)
def _clean_state():
    rs.reset_for_tests()
    yield
    rs.reset_for_tests()


def _envelope(**over) -> dict:
    base = {
        "name": "home-lights",
        "version": "v1",
        "mode": "replace",
        "instructions": "---\nname: home-lights\ndescription: served\n---\n\n# Lights\n\nrooms.\n",
        "python": "def room_on(room): ...\n",
    }
    base.update(over)
    return base


# ── discovery ────────────────────────────────────────────────────────────────

def test_discovery_derives_the_endpoint_from_the_service_url() -> None:
    found = rs.discover_endpoints({"CAMERA_API_URL": "http://hub.home:21100/api"})
    assert found == {"camera-feed": "http://hub.home:21100/api/skill"}


def test_discovery_ignores_a_service_with_no_address() -> None:
    assert rs.discover_endpoints({"CAMERA_API_URL": "   "}) == {}
    assert rs.discover_endpoints({}) == {}


def test_discovery_applies_the_path_swap_for_services_sharing_a_host() -> None:
    found = rs.discover_endpoints({"TASKS_API_URL": "https://hub.home:8443/tasks/api"})
    assert found["tasks"] == "https://hub.home:8443/tasks/api/skill"
    assert found["grocery"] == "https://hub.home:8443/grocery/api/skill"
    assert found["geo"] == "https://hub.home:8443/geo/api/skill"


def test_discovery_skips_a_swap_that_does_not_apply() -> None:
    # A tasks URL that is not spelled the way the rewrite expects means the
    # derived siblings would be guesses. Better to have no endpoint than a
    # fabricated one.
    found = rs.discover_endpoints({"TASKS_API_URL": "https://elsewhere.example/v2"})
    assert "grocery" not in found
    assert found["tasks"] == "https://elsewhere.example/v2/skill"


def test_discovery_refuses_a_non_http_address() -> None:
    assert rs.discover_endpoints({"CAMERA_API_URL": "file:///etc/passwd"}) == {}


# ── composing ────────────────────────────────────────────────────────────────

def test_replace_uses_the_served_document_whole() -> None:
    out = rs.compose("home-lights", _envelope(), bundled="---\nname: home-lights\n---\n\n# Old\n")
    assert "# Lights" in out and "# Old" not in out
    assert "description: served" in out


def test_append_keeps_the_bundled_skill_and_adds_to_it() -> None:
    bundled = "---\nname: home-lights\ndescription: bundled\n---\n\n# Lights\n\nthe basics.\n"
    out = rs.compose("home-lights", _envelope(
        mode="append", instructions="## Rooms in this house\n\n- living\n"), bundled)
    assert "the basics." in out
    assert "## Rooms in this house" in out
    assert "description: bundled" in out


def test_append_with_nothing_bundled_still_produces_a_usable_skill() -> None:
    # A service introducing a skill nanobot has never heard of gets it whether
    # or not it remembered to say "replace".
    out = rs.compose("newthing", _envelope(mode="append", instructions="# New\n"), bundled=None)
    assert "# New" in out
    assert out.startswith("---")


def test_a_served_description_overrides_the_bundled_one() -> None:
    bundled = "---\nname: home-lights\ndescription: bundled\n---\n\n# Lights\n"
    out = rs.compose("home-lights", _envelope(
        mode="append", description="now with rooms", instructions="extra"), bundled)
    assert "now with rooms" in out and "description: bundled" not in out


def test_instructions_without_frontmatter_get_some() -> None:
    out = rs.compose("home-lights", _envelope(instructions="# Just a body\n"), bundled=None)
    front, body = rs._split_frontmatter(out)
    assert front["name"] == "home-lights"
    assert front["description"]              # never blank; it is what the model reads first
    assert "# Just a body" in body


def test_frontmatter_metadata_survives_the_round_trip() -> None:
    served = ('---\nname: home-lights\ndescription: d\n'
              'metadata: {"nanobot":{"always":true}}\n---\n\n# Lights\n')
    out = rs.compose("home-lights", _envelope(instructions=served), bundled=None)
    front, _ = rs._split_frontmatter(out)
    meta = front["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    assert meta["nanobot"]["always"] is True


# ── fetching ─────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_fetch_rejects_a_payload_with_no_instructions(monkeypatch) -> None:
    monkeypatch.setattr(rs.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(json.dumps({"name": "x"}).encode()))
    assert rs.fetch("http://svc/skill") is None


def test_fetch_rejects_something_that_is_not_json(monkeypatch) -> None:
    monkeypatch.setattr(rs.urllib.request, "urlopen", lambda *a, **k: _Resp(b"<html>login</html>"))
    assert rs.fetch("http://svc/skill") is None


def test_fetch_rejects_an_oversized_document(monkeypatch) -> None:
    huge = json.dumps({"instructions": "x" * (rs._MAX_BYTES + 10)}).encode()
    monkeypatch.setattr(rs.urllib.request, "urlopen", lambda *a, **k: _Resp(huge))
    assert rs.fetch("http://svc/skill") is None


def test_fetch_treats_unreachable_as_no_answer(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(rs.urllib.request, "urlopen", boom)
    assert rs.fetch("http://svc/skill") is None


def test_fetch_distinguishes_a_404_from_a_failure(monkeypatch) -> None:
    def gone(*a, **k):
        raise rs.urllib.error.HTTPError("http://svc/skill", 404, "no", {}, None)
    monkeypatch.setattr(rs.urllib.request, "urlopen", gone)
    with pytest.raises(rs.SkillGone):
        rs.fetch("http://svc/skill")


# ── materialising and syncing ────────────────────────────────────────────────

def _stub_fetch(monkeypatch, answer):
    def fake(url, timeout=None):
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(rs, "fetch", fake)


def test_sync_writes_both_halves_of_the_skill(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    out = rs.sync_once(tmp_path, tmp_path / "builtin", {"home-lights": "http://svc/skill"})
    assert out == {"home-lights": "updated"}
    cached = rs.cache_dir(tmp_path) / "home-lights"
    assert "# Lights" in (cached / "SKILL.md").read_text()
    assert "def room_on" in (cached / "SKILL_PYTHON.md").read_text()


def test_an_unchanged_version_is_not_rewritten(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    endpoints = {"home-lights": "http://svc/skill"}
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    assert rs.sync_once(tmp_path, tmp_path / "builtin", endpoints) == {"home-lights": "unchanged"}


def test_a_new_version_is_picked_up(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    endpoints = {"home-lights": "http://svc/skill"}
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    _stub_fetch(monkeypatch, _envelope(version="v2", instructions="# Lights v2\n"))
    assert rs.sync_once(tmp_path, tmp_path / "builtin", endpoints) == {"home-lights": "updated"}
    assert "v2" in (rs.cache_dir(tmp_path) / "home-lights" / "SKILL.md").read_text()


def test_a_service_that_is_down_leaves_the_last_copy_alone(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    endpoints = {"home-lights": "http://svc/skill"}
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    monkeypatch.setattr(rs, "fetch", lambda url, timeout=None: None)
    assert rs.sync_once(tmp_path, tmp_path / "builtin", endpoints) == {"home-lights": "unreachable"}
    # Still there: an unplugged light service must not also cost Alfred the
    # ability to talk about lights.
    assert (rs.cache_dir(tmp_path) / "home-lights" / "SKILL.md").is_file()


def test_a_404_withdraws_the_skill(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    endpoints = {"home-lights": "http://svc/skill"}
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    _stub_fetch(monkeypatch, rs.SkillGone("http://svc/skill"))
    assert rs.sync_once(tmp_path, tmp_path / "builtin", endpoints) == {"home-lights": "gone"}
    assert not (rs.cache_dir(tmp_path) / "home-lights").exists()


def test_a_skill_no_row_names_any_more_is_dropped(tmp_path: Path, monkeypatch) -> None:
    # The row went, so the service is never asked and can never answer 404.
    # The loader reads the directory, not the table: without this the skill
    # stayed in every prompt. home-lights, 2026-09-11.
    _stub_fetch(monkeypatch, _envelope())
    rs.sync_once(tmp_path, tmp_path / "builtin",
                 {"home-lights": "http://svc/skill", "tasks": "http://svc/skill"})
    out = rs.sync_once(tmp_path, tmp_path / "builtin", {"tasks": "http://svc/skill"})
    assert out == {"tasks": "unchanged", "home-lights": "gone"}
    assert not (rs.cache_dir(tmp_path) / "home-lights").exists()
    assert (rs.cache_dir(tmp_path) / "tasks" / "SKILL.md").is_file()


def test_a_named_skill_whose_service_is_down_is_not_unclaimed(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    endpoints = {"home-lights": "http://svc/skill"}
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    monkeypatch.setattr(rs, "fetch", lambda url, timeout=None: None)
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    assert (rs.cache_dir(tmp_path) / "home-lights" / "SKILL.md").is_file()


def test_dropping_the_python_guide_removes_the_stale_one(tmp_path: Path, monkeypatch) -> None:
    _stub_fetch(monkeypatch, _envelope())
    endpoints = {"home-lights": "http://svc/skill"}
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    _stub_fetch(monkeypatch, _envelope(version="v2", python=None))
    rs.sync_once(tmp_path, tmp_path / "builtin", endpoints)
    assert not (rs.cache_dir(tmp_path) / "home-lights" / "SKILL_PYTHON.md").exists()


def test_sync_survives_one_broken_service(tmp_path: Path, monkeypatch) -> None:
    def fake(url, timeout=None):
        if "bad" in url:
            raise RuntimeError("kaboom")
        return _envelope()
    monkeypatch.setattr(rs, "fetch", fake)
    out = rs.sync_once(tmp_path, tmp_path / "builtin",
                       {"home-lights": "http://svc/skill", "tasks": "http://bad/skill"})
    assert out == {"home-lights": "updated", "tasks": "error"}


# ── what the loader ends up seeing ───────────────────────────────────────────

def _bundled(tmp_path: Path, name: str, description: str) -> Path:
    builtin = tmp_path / "builtin"
    (builtin / name).mkdir(parents=True, exist_ok=True)
    (builtin / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Bundled\n", encoding="utf-8")
    return builtin


def test_a_served_skill_beats_the_bundled_one(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = _bundled(tmp_path, "home-lights", "bundled")
    _stub_fetch(monkeypatch, _envelope())
    rs.sync_once(workspace, builtin, {"home-lights": "http://svc/skill"})

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=False)
    assert [(e["name"], e["source"]) for e in entries] == [("home-lights", "remote")]
    assert "# Lights" in loader.load_skill("home-lights")
    assert loader.get_skill_metadata("home-lights")["description"] == "served"


def test_a_workspace_override_still_beats_a_served_skill(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills" / "home-lights").mkdir(parents=True)
    (workspace / "skills" / "home-lights" / "SKILL.md").write_text(
        "---\nname: home-lights\ndescription: mine\n---\n\n# Mine\n", encoding="utf-8")
    builtin = _bundled(tmp_path, "home-lights", "bundled")
    _stub_fetch(monkeypatch, _envelope())
    rs.sync_once(workspace, builtin, {"home-lights": "http://svc/skill"})

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    entries = loader.list_skills(filter_unavailable=False)
    assert [(e["name"], e["source"]) for e in entries] == [("home-lights", "workspace")]
    assert "# Mine" in loader.load_skill("home-lights")


def test_a_service_can_introduce_a_skill_nobody_bundled(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    _stub_fetch(monkeypatch, _envelope(name="doorbell", instructions="# Doorbell\n"))
    rs.sync_once(workspace, builtin, {"doorbell": "http://svc/skill"})

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    assert [e["name"] for e in loader.list_skills(filter_unavailable=False)] == ["doorbell"]
    assert "# Doorbell" in loader.load_skill("doorbell")


def test_the_summary_points_at_the_served_file(tmp_path: Path, monkeypatch) -> None:
    # The invocation interceptor maps a {"skill": …} block back to a path taken
    # from this line, and _load_skill_python_guide looks for SKILL_PYTHON.md
    # beside it. If the summary still pointed at the bundled copy, the served
    # skill would be described to the model and then executed from the old code.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = _bundled(tmp_path, "home-lights", "bundled")
    _stub_fetch(monkeypatch, _envelope())
    rs.sync_once(workspace, builtin, {"home-lights": "http://svc/skill"})

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    summary = loader.build_skills_summary()
    assert str(rs.cache_dir(workspace) / "home-lights" / "SKILL.md") in summary


def test_a_disabled_skill_stays_disabled_when_served(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    _stub_fetch(monkeypatch, _envelope())
    rs.sync_once(workspace, builtin, {"home-lights": "http://svc/skill"})

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin, disabled_skills={"home-lights"})
    assert loader.list_skills(filter_unavailable=False) == []


def test_nothing_is_started_when_no_service_has_an_address(tmp_path: Path, monkeypatch) -> None:
    for _, var, _ in rs.SERVICE_SKILL_ENV:
        monkeypatch.delenv(var, raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    SkillsLoader(workspace, builtin_skills_dir=tmp_path / "builtin")
    assert not rs.cache_dir(workspace).exists()


def test_a_cold_start_does_not_wait_out_a_hung_service(tmp_path: Path, monkeypatch) -> None:
    # Boot must not be held hostage by a service that accepts the connection and
    # then says nothing. The bundled skills cover the wait; a container that
    # takes twenty seconds to answer its first message does not.
    import time
    monkeypatch.setenv("HOME_LIGHTS_API_URL", "http://hub.home:5010/api")
    monkeypatch.setattr(rs.RemoteSkillSync, "COLD_START_WAIT_S", 0.2)
    monkeypatch.setattr(rs, "sync_once", lambda *a, **k: time.sleep(5) or {})
    workspace = tmp_path / "ws"
    workspace.mkdir()

    started = time.monotonic()
    SkillsLoader(workspace, builtin_skills_dir=tmp_path / "builtin")
    assert time.monotonic() - started < 2.0


def test_a_warm_cache_does_not_block_at_all(tmp_path: Path, monkeypatch) -> None:
    import time
    monkeypatch.setenv("HOME_LIGHTS_API_URL", "http://hub.home:5010/api")
    monkeypatch.setattr(rs.RemoteSkillSync, "COLD_START_WAIT_S", 30.0)
    monkeypatch.setattr(rs, "sync_once", lambda *a, **k: time.sleep(5) or {})
    workspace = tmp_path / "ws"
    (workspace / rs.CACHE_DIRNAME).mkdir(parents=True)

    started = time.monotonic()
    SkillsLoader(workspace, builtin_skills_dir=tmp_path / "builtin")
    assert time.monotonic() - started < 1.0


def test_the_feature_can_be_switched_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME_LIGHTS_API_URL", "http://hub.home:5010/api")
    monkeypatch.setenv("NANOBOT_REMOTE_SKILLS", "0")
    called = []
    monkeypatch.setattr(rs, "sync_once", lambda *a, **k: called.append(1) or {})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    SkillsLoader(workspace, builtin_skills_dir=tmp_path / "builtin")
    assert called == []


# --- rows this image does not ship ------------------------------------------
#
# A household's own service cannot be listed in SERVICE_SKILL_ENV: the core does
# not know it exists. The deployer passes its rows in, one per plugin that
# declares a skill, and they behave exactly like a shipped row.

def test_a_row_supplied_at_run_time_is_discovered() -> None:
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "switches=SWITCHES_API_URL",
        "SWITCHES_API_URL": "http://hub.home:8095",
    })
    assert found == {"switches": "http://hub.home:8095/skill"}


def test_a_supplied_row_can_rewrite_the_path_like_a_shipped_one() -> None:
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "solar=SOLAR_URL:/api>/skill-api",
        "SOLAR_URL": "http://solar.home/api",
    })
    assert found == {"solar": "http://solar.home/skill-api/skill"}


def test_a_supplied_row_cannot_take_over_a_shipped_skill() -> None:
    """Otherwise a plugin could quietly answer for `paperless`, and every
    document question in the house would go to somebody else's service."""
    found = rs.discover_endpoints({
        "PAPERLESS_URL": "http://paperless.home:8000",
        "NANOBOT_SKILL_SERVICES": "paperless=IMPOSTOR_URL",
        "IMPOSTOR_URL": "http://impostor.home",
    })
    assert found == {"paperless": "http://paperless.home:8000/skill"}


def test_a_row_whose_variable_is_unset_probes_nothing() -> None:
    assert rs.discover_endpoints({"NANOBOT_SKILL_SERVICES": "x=X_URL"}) == {}


def test_a_malformed_row_is_dropped_rather_than_fatal() -> None:
    """Read at startup. A typo in one household service must not stop the
    assistant answering about any of the others."""
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": ",,broken,=X,name=,good=GOOD_URL,",
        "GOOD_URL": "http://good.home",
        "X": "http://x.home",
    })
    assert found == {"good": "http://good.home/skill"}


def test_no_supplied_rows_is_exactly_the_old_behaviour() -> None:
    plain = {"PAPERLESS_URL": "http://paperless.home:8000"}
    assert rs.discover_endpoints(plain) == rs.discover_endpoints(
        dict(plain, NANOBOT_SKILL_SERVICES=""))


# --- a supplied row is a name, and only a name -------------------------------
#
# `name` was taken verbatim from NANOBOT_SKILL_SERVICES and used as a path
# component: `cache_dir(workspace) / name`. Two things follow, and neither is
# theoretical. A name containing `..` walks out of the cache -- and one landing
# spot is `<workspace>/skills/`, which outranks both the cache and the builtin
# copy, so the "a local override cannot be defeated by a remote service"
# guarantee in skills.py goes with it. An absolute name discards the prefix
# outright. What gets written there is not inert prose either: the runner pulls
# the first ```python fence out of SKILL_PYTHON.md and executes it with the
# exec tool's credentials.


@pytest.mark.parametrize("name", [
    "../skills/file-share",
    "../../etc/cron.d/x",
    "/tmp/anywhere",
    "a/b",
    ".",
    "..",
    "",
])
def test_a_supplied_name_that_is_not_a_name_is_dropped(name: str) -> None:
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": f"{name}=PLUGIN_URL",
        "PLUGIN_URL": "http://plugin.home",
    })
    assert found == {}


def test_the_write_refuses_a_traversing_name_even_if_one_reaches_it(
        tmp_path: Path) -> None:
    """The parser is the fix; this is the assertion at the sink, so a reader of
    `materialize` does not have to go and find the parser to know it is bounded."""
    outside = tmp_path / "skills" / "file-share"
    assert not rs.materialize(tmp_path, "../skills/file-share",
                              {"version": "1", "instructions": "hi"}, None)
    assert not outside.exists()


def test_prune_refuses_a_traversing_name(tmp_path: Path) -> None:
    """A 404 from one endpoint must not unlink somebody else's directory."""
    victim = tmp_path / "skills" / "file-share"
    victim.mkdir(parents=True)
    (victim / "SKILL.md").write_text("the operator's own override")
    # The cache has to exist for the traversal to resolve at all -- without it
    # `is_dir()` is False for the wrong reason and the test passes while the
    # hole is open.
    rs.cache_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    assert not rs.prune(tmp_path, "../skills/file-share")
    assert (victim / "SKILL.md").is_file()


# --- and it may not be a name this package already ships ---------------------


def test_a_shipped_skill_is_held_even_when_its_own_variable_is_unset() -> None:
    """The old guard was `if skill in found`, and `found` only holds rows that
    resolved to a URL. On nanobot-house TASKS_API_URL and PAPERLESS_URL are
    deliberately absent -- which is exactly where an unguarded claim landed."""
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "paperless=IMPOSTOR_URL",
        "IMPOSTOR_URL": "http://impostor.home",
    })
    assert found == {}


@pytest.mark.parametrize("name", ["notifications", "memory", "document",
                                  "whatsapp", "theme", "cron"])
def test_a_shipped_skill_outside_the_table_is_held_too(name: str) -> None:
    """SERVICE_SKILL_ENV names 11 skills; the package ships 27. The dozen-odd
    that no row mentions were defended by nothing at all."""
    assert name in rs._shipped_skill_names()
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": f"{name}=IMPOSTOR_URL",
        "IMPOSTOR_URL": "http://impostor.home",
    })
    assert found == {}


def test_a_name_the_package_does_not_ship_still_works() -> None:
    """The guards must not cost a household its own service."""
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "switches=SWITCHES_API_URL",
        "SWITCHES_API_URL": "http://switches.home:8095",
    })
    assert found == {"switches": "http://switches.home:8095/skill"}


# --- the house's own certificate --------------------------------------------
# HomeCore serves a self-signed cert on the home network and every household
# skill is fetched from it. Verifying it fails, so tasks, grocery, menu, geo,
# files and chat all came back CERTIFICATE_VERIFY_FAILED on every refresh --
# at DEBUG, so nothing surfaced. The agent, left without the household's own
# skills, reached for a `direct-curl-execution` workaround whose stated purpose
# was "bypassing skill import errors", and that hit the exec guard for internal
# URLs. Two layers of symptom over one unverifiable certificate.

def test_the_houses_own_services_are_not_verified():
    """The addresses this household serves itself on."""
    for url in (
        "https://host.docker.internal:21001/tasks/api/skill",
        "https://127.0.0.1:21001/grocery/api/skill",
        "https://192.168.1.5/menu/api/skill",
        "https://10.0.0.2/files/api/skill",
        "https://portal.home/chat/api/skill",
        "https://localhost:21001/geo/api/skill",
    ):
        assert rs._ssl_context_for(url) is not None, url


def test_anything_else_still_has_to_prove_who_it_is():
    """`fetch` takes whatever URL a service declares, so the exemption is a fact
    about the house rather than a decision to stop checking. A skill served from
    the public internet is not covered by the household's self-signed cert."""
    for url in (
        "https://api.example.com/skill",
        "https://skills.vendor.io/v1/skill",
        "https://8.8.8.8/skill",
    ):
        assert rs._ssl_context_for(url) is None, url


def test_a_configured_name_is_the_house_whatever_its_suffix(monkeypatch):
    """`dns:` is a setting, and the manifest builds TASKS_API_URL out of it.

    The suffix list can only ever know the suffixes somebody thought of, so a
    household whose domain is not one of them had every household skill fail
    CERTIFICATE_VERIFY_FAILED against the certificate its own local CA issued
    -- the exact failure `_ssl_context_for` was written for, arriving by a name
    instead of by an address."""
    monkeypatch.setenv("TASKS_API_URL", "https://casa.midominio.net:21001/tasks/api")
    assert rs._ssl_context_for(
        "https://casa.midominio.net:21001/grocery/api/skill") is not None


def test_a_name_the_deployer_never_supplied_still_has_to_prove_itself(monkeypatch):
    """Otherwise the exemption is "any host", and a skill served from the
    public internet would be trusted for being asked about."""
    monkeypatch.setenv("TASKS_API_URL", "https://casa.midominio.net:21001/tasks/api")
    assert rs._ssl_context_for("https://impostor.example.com/skill") is None


def test_plain_http_needs_no_context():
    """There is nothing to verify, and handing urlopen a TLS context for an
    http:// URL is a way to be surprised later."""
    assert rs._ssl_context_for("http://n8n.home:5678/skill") is None


def test_a_house_address_that_is_not_an_address_is_not_the_house():
    """`_is_house` parses before it decides. A hostname that merely contains a
    private-looking string is not one, and treating it as the house would turn
    verification off for somebody else's server."""
    assert not rs._is_house("10.0.0.2.evil.example.com")
    assert not rs._is_house("nothost.docker.internal.example.com")
    assert not rs._is_house("")


# --- a plugin's floor is not a skill this package ships -----------------------
# The two halves of the plugin-skill feature refused each other. A floor is
# staged into the *builtin* directory on purpose, because SkillsLoader reads
# workspace, then remote, then builtin, and builtin is the only tier where the
# live copy the plugin's own service serves still wins. But `_shipped_skill_names`
# derived "ships" from that same directory at runtime, after the deployer had
# staged the floor into it -- so the floor made the remote entry a name
# collision, the entry was dropped, and the floor became the only copy.
#
# Observed, not theorised: every instance logged
#   Ignoring NANOBOT_SKILL_SERVICES entry 'backups=BACKUPS_API_URL':
#   'backups' is a skill this package ships
# while the service answering /skill sat right there.

def _make_floor(tmp_path, name, marker=True):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# floor copy\n", encoding="utf-8")
    if marker:
        (d / rs._PLUGIN_FLOOR_MARKER).write_text("staged by the deployer\n",
                                                 encoding="utf-8")
    return d


def test_a_marked_plugin_floor_is_not_counted_as_shipped(tmp_path, monkeypatch):
    _make_floor(tmp_path, "backups")
    monkeypatch.setattr(rs, "_BUILTIN_SKILLS_DIR", tmp_path)
    assert "backups" not in rs._shipped_skill_names()


def test_the_remote_half_survives_its_own_floor(tmp_path, monkeypatch):
    """The whole point: the floor exists *and* the live service is reachable."""
    _make_floor(tmp_path, "backups")
    monkeypatch.setattr(rs, "_BUILTIN_SKILLS_DIR", tmp_path)
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "backups=BACKUPS_API_URL",
        "BACKUPS_API_URL": "http://127.0.0.1:21601",
    })
    assert found == {"backups": "http://127.0.0.1:21601/skill"}


def test_an_unmarked_directory_still_counts_as_shipped(tmp_path, monkeypatch):
    """The marker is the only thing that exempts a directory. Without it this
    is an ordinary shipped skill and the impostor guard must still hold --
    otherwise the fix would have handed away every name it was defending."""
    _make_floor(tmp_path, "notifications", marker=False)
    monkeypatch.setattr(rs, "_BUILTIN_SKILLS_DIR", tmp_path)
    assert "notifications" in rs._shipped_skill_names()
    found = rs.discover_endpoints({
        "NANOBOT_SKILL_SERVICES": "notifications=IMPOSTOR_URL",
        "IMPOSTOR_URL": "http://impostor.home",
    })
    assert found == {}


# --- a polite "no" is not a broken envelope ----------------------------------
# The JSON guard above stopped one case short. A service that does not implement
# /skill may still answer *JSON* at it: paperless returns its login form as a
# well-formed {"form": ..., "html": ...} when asked with Accept:
# application/json, 200 and all. Every instance warned about that on every
# refresh, forever, which is exactly the noise the JSON branch exists to stop.

def _serve(monkeypatch, payload: bytes):
    class _Resp:
        def read(self, *_a): return payload
        def __enter__(self): return self
        def __exit__(self, *_a): return False
    monkeypatch.setattr(rs.urllib.request, "urlopen", lambda *a, **k: _Resp())


def _warnings_from(monkeypatch, payload: bytes, url: str) -> list[str]:
    """Warnings loguru emitted for one fetch. `caplog` sees none of these --
    nanobot logs through loguru, which does not propagate to stdlib logging,
    so a caplog-based assertion passes whatever the code does."""
    from loguru import logger as loguru_logger

    _serve(monkeypatch, payload)
    seen: list[str] = []
    handler = loguru_logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        assert rs.fetch(url) is None
    finally:
        loguru_logger.remove(handler)
    return [line for line in seen if "has no instructions" in line]


def test_a_login_page_that_speaks_json_is_not_warned_about(monkeypatch):
    assert not _warnings_from(
        monkeypatch, b'{"form": {"fields": {}}, "html": "<form/>"}',
        "http://paperless.home:21030/skill")


def test_an_envelope_that_forgot_its_instructions_still_warns(monkeypatch):
    """The case the warning was written for: a household's own service trying
    to serve a skill and getting it wrong. That is worth seeing."""
    assert _warnings_from(
        monkeypatch, b'{"name": "home-lights", "version": "3f9c", "mode": "replace"}',
        "http://lights.home:8095/skill")


def test_a_good_envelope_is_still_returned(monkeypatch):
    _serve(monkeypatch, b'{"name": "x", "instructions": "# do the thing\\n"}')
    got = rs.fetch("http://x.home/skill")
    assert got and got["instructions"].startswith("# do the thing")
