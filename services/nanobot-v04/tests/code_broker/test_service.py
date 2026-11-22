"""Who the broker will act for, and on which projects.

The agent cannot be trusted to say who it is — that is why this process exists
— so two things have to hold no matter what any prompt says. An instance can
present a token for itself and for nobody else. And whether a user may touch a
project is a question the registry answers, never one the caller asserts.

The third property here is quieter and matters as much: a registry that is
*down* must not look like a project that does not exist. One is worth retrying;
the other never is, and confusing them is how an outage turns into "Alfred says
the project was deleted".
"""
import json

import asyncio
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from nanobot.code_broker.service import Broker, create_app, user_token
from nanobot.code_broker.workspace import MARKER, Workspace

SECRET = "s" * 40
BROKER_TOKEN = "b" * 40
# Two identifiers per person, and both are load-bearing: the instance names
# the checkout directory, the HomeWeb username names the access list.
MEMBER_A, MEMBER_B = "user2", "user3"
USERS = {MEMBER_A: "user2", MEMBER_B: "user3"}
# What HomeWeb answers `display_name` with, keyed the way the registry keys
# it: by login id, not by instance.
DISPLAY_NAMES = {"user2": "Member Two", "user3": "Member Three"}


@pytest.fixture
def registry():
    """A stand-in HomeWeb, so the access rules under test are the broker's."""
    state = {"projects": {}, "status": 200, "down": False, "seen": [], "asked": []}

    async def _list(request):
        state["seen"].append(request.headers.get("X-Broker-Token"))
        state["asked"].append(request.match_info["user"])
        if state["down"]:
            raise web.HTTPInternalServerError()
        user = request.match_info["user"]
        return web.json_response({"projects": [
            p["project"] for p in state["projects"].values() if user in p["access"]
        ]})

    async def _one(request):
        state["seen"].append(request.headers.get("X-Broker-Token"))
        if state["down"]:
            raise web.HTTPInternalServerError()
        user, slug = request.match_info["user"], request.match_info["slug"]
        entry = state["projects"].get(slug)
        if not entry or user not in entry["access"]:
            return web.json_response({"error": "proyecto desconocido"}, status=404)
        return web.json_response({
            "project": entry["project"],
            "auth_kind": entry.get("auth_kind", "none"),
            "auth_secret": entry.get("auth_secret", ""),
            "display_name": DISPLAY_NAMES.get(user, user),
        })

    app = web.Application()
    app.router.add_get("/projects/api/broker/{user}", _list)
    app.router.add_get("/projects/api/broker/{user}/{slug}", _one)
    app.state = state
    return app


@pytest_asyncio.fixture
async def started(registry):
    """The stand-in registry, listening, and torn down after."""
    server = TestServer(registry)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


@pytest_asyncio.fixture
async def client(started, registry, tmp_path, origin):
    server = started
    root = tmp_path / "nanobot-code-workspace"
    root.mkdir()
    (root / MARKER).write_text("test\n", encoding="utf-8")

    registry.state["projects"]["demo"] = {
        "access": [USERS[MEMBER_A]],
        "project": {"slug": "demo", "name": "Demo", "git_url": str(origin),
                    "verify_path": "deploy/verify.sh", "host": "hub"},
    }
    broker = Broker(Workspace(root), str(server.make_url("")), BROKER_TOKEN,
                    SECRET, users=dict(USERS))
    api = TestClient(TestServer(create_app(broker)))
    await api.start_server()
    api.registry = registry.state
    api.broker = broker
    try:
        yield api
    finally:
        await api.close()


@pytest.fixture
def origin(tmp_path):
    import subprocess
    repo = tmp_path / "origin"
    repo.mkdir()

    def run(*a):
        subprocess.run(a, cwd=repo, check=True, capture_output=True)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "T")
    (repo / "README.md").write_text("hola\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "first")
    return repo


def _as(user):
    return {"X-Code-User": user, "X-Code-Token": user_token(SECRET, user)}


# --- who is asking ----------------------------------------------------------

@pytest.mark.asyncio
async def test_no_token_is_refused(client):
    assert (await client.get("/v1/projects")).status == 401


@pytest.mark.asyncio
async def test_a_token_for_one_user_does_not_work_for_another(client):
    """The property that makes per-user isolation real: an instance holds its
    own token, and holding it proves nothing about anybody else."""
    headers = {"X-Code-User": MEMBER_B, "X-Code-Token": user_token(SECRET, MEMBER_A)}

    assert (await client.get("/v1/projects", headers=headers)).status == 401


@pytest.mark.asyncio
async def test_a_made_up_token_is_refused(client):
    headers = {"X-Code-User": MEMBER_A, "X-Code-Token": "f" * 64}
    assert (await client.get("/v1/projects", headers=headers)).status == 401


@pytest.mark.asyncio
async def test_without_a_configured_secret_everything_is_refused(client):
    """Unset, every request fails closed rather than every request passing."""
    client.broker.client_secret = ""

    assert (await client.get("/v1/projects", headers=_as(MEMBER_A))).status == 503


# --- what the registry says -------------------------------------------------

@pytest.mark.asyncio
async def test_an_instance_nobody_configured_is_refused(client):
    """A sixth container appearing must not silently inherit everybody's
    projects: it is unknown here before any registry call is made."""
    client.broker.users.pop(MEMBER_B)

    assert (await client.get("/v1/projects", headers=_as(MEMBER_B))).status == 401


@pytest.mark.asyncio
async def test_the_registry_is_asked_about_the_homeweb_username(client):
    """The instance names the directory; the registry knows the person by the
    name users.json gives them, and only the broker resolves between them."""
    await client.get("/v1/projects", headers=_as(MEMBER_A))

    assert client.registry["asked"] == [USERS[MEMBER_A]]


@pytest.mark.asyncio
async def test_the_list_is_the_registry_answer_for_that_user(client):
    body = await (await client.get("/v1/projects", headers=_as(MEMBER_A))).json()
    assert [p["slug"] for p in body["projects"]] == ["demo"]

    body = await (await client.get("/v1/projects", headers=_as(MEMBER_B))).json()
    assert body["projects"] == []


@pytest.mark.asyncio
async def test_a_project_the_user_may_not_see_is_a_404(client):
    resp = await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_B))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_the_broker_presents_its_own_credential_to_the_registry(client):
    await client.get("/v1/projects", headers=_as(MEMBER_A))
    assert client.registry["seen"] == [BROKER_TOKEN]


@pytest.mark.asyncio
async def test_a_registry_that_is_down_is_a_502_not_a_404(client):
    """One is worth retrying and the other never is. Confusing them is how an
    outage becomes "Alfred says the project was deleted"."""
    client.registry["down"] = True

    resp = await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))
    assert resp.status == 502
    assert "registry" in (await resp.json())["error"]


# --- the verbs --------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_checkout_lands_under_the_calling_user(client, tmp_path):
    body = await (await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))).json()

    assert body["action"] == "cloned"
    assert body["path"].endswith(f"/{MEMBER_A}/demo")
    assert body["branch"] == "main"


@pytest.mark.asyncio
async def test_the_checkout_answer_carries_the_project_it_checked_out(client):
    """Learned in the same breath as the path: which host it deploys on, and
    whether a failed deploy could roll itself back. Not whether it may be
    merged unattended — nothing may."""
    body = await (await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))).json()
    assert body["project"]["host"] == "hub"
    assert body["project"]["verify_path"] == "deploy/verify.sh"


@pytest.mark.asyncio
async def test_status_before_a_checkout_is_a_404(client):
    resp = await client.get("/v1/projects/demo/status", headers=_as(MEMBER_A))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_status_after_one_reports_the_tree(client):
    await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))
    body = await (await client.get("/v1/projects/demo/status", headers=_as(MEMBER_A))).json()

    assert body["branch"] == "main" and body["dirty"] is False


@pytest.mark.asyncio
async def test_status_checks_access_before_it_looks_at_the_disk(client):
    """Otherwise a checkout somebody else's agent made would be reportable by
    slug alone."""
    await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))
    resp = await client.get("/v1/projects/demo/status", headers=_as(MEMBER_B))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_health_says_whether_the_workspace_is_real(client):
    body = await (await client.get("/health")).json()
    assert body["ok"] is True and body["workspace_marked"] is True


@pytest.mark.asyncio
async def test_an_unexpected_error_is_not_a_traceback_on_the_wire(client, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("something private about the host")

    monkeypatch.setattr("nanobot.code_broker.service.checkout", boom)
    resp = await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))

    assert resp.status == 500
    assert "private" not in json.dumps(await resp.json())


# --- the skill the service hands out for itself -----------------------------
#
# `agent/remote_skills.py` explains the shape: a skill is a description of
# somebody else's HTTP API, and when the two live behind different deploy jobs
# the description drifts. Serving it from here means the instructions ship with
# the verbs.

@pytest.mark.asyncio
async def test_the_skill_is_served_without_a_token(client):
    """It describes a contract and names no project and no person. Gating it
    would mean needing a credential to learn how to ask for one."""
    resp = await client.get("/skill")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_the_envelope_is_the_shape_remote_skills_expects(client):
    body = await (await client.get("/skill")).json()

    assert body["name"] == "code"
    assert body["mode"] in ("replace", "append")
    assert body["version"] and len(body["version"]) >= 8
    assert body["instructions"].lstrip().startswith("---"), "frontmatter first"
    assert "Invoke with JSON" in body["instructions"]


@pytest.mark.asyncio
async def test_the_version_changes_only_when_the_content_does(client):
    first = (await (await client.get("/skill")).json())["version"]
    second = (await (await client.get("/skill")).json())["version"]
    assert first == second, "an unchanged answer must not churn the instance's files"


@pytest.mark.asyncio
async def test_the_instructions_say_the_shell_cannot_reach_a_remote(client):
    """Otherwise the first task spends itself debugging what looks like a
    network fault and is actually a rule."""
    text = (await (await client.get("/skill")).json())["instructions"]

    # The shell verbs that will fail, named so the failure is recognisable.
    assert "git clone" in text and "git push" in text and "git fetch" in text
    assert "credenciales" in text
    # And what to use instead, which is the half that makes it actionable.
    assert "checkout" in text
    for action in ("branch", "commit", "push", "pull"):
        assert action in text, f"{action} is a verb now; the instructions must say so"


@pytest.mark.asyncio
async def test_the_instructions_say_the_agent_never_merges(client):
    """The one rule that cannot be a preference. `commit` and `push` enforce it
    in workspace.py, and an agent that has not been told will spend a task
    finding out from a 403."""
    text = (await (await client.get("/skill")).json())["instructions"]

    assert "no mergeas" in text.lower() or "mergear" in text
    assert "main" in text


@pytest.mark.asyncio
async def test_the_instructions_say_not_to_read_the_whole_thing(client):
    """437k lines against a context that holds a fraction of it. The map is
    the useful part, and every repo already has one."""
    text = (await (await client.get("/skill")).json())["instructions"]

    assert "AGENTS.md" in text
    assert "grep" in text


@pytest.mark.asyncio
async def test_the_python_guide_reads_the_variables_exec_is_allowed(client):
    """A guide that reads a variable the exec tool filters out is a guide that
    fails at runtime with an empty string — allowedEnvKeys in the deployed
    config has to carry these three."""
    guide = (await (await client.get("/skill")).json())["python"]

    for var in ("CODE_BROKER_URL", "CODE_BROKER_TOKEN", "NANOBOT_INSTANCE"):
        assert var in guide, var
    assert "def list_projects" in guide
    assert "def checkout" in guide
    assert "def project_status" in guide


# --- the git verbs, at the door ---------------------------------------------
#
# workspace.py holds the rules; these are about the boundary in front of them.
# Every verb has to answer the same three questions the older ones do — who is
# asking, may they see this project, and is the registry the one saying so —
# and `commit` has a fourth: the protected paths are the registry's answer, not
# the agent's, so they have to come from there on every call.

@pytest.mark.parametrize("verb", ["branch", "commit", "push", "pull"])
@pytest.mark.asyncio
async def test_a_git_verb_without_a_token_is_refused(client, verb):
    assert (await client.post(f"/v1/projects/demo/{verb}")).status == 401


@pytest.mark.parametrize("verb", ["branch", "commit", "push", "pull"])
@pytest.mark.asyncio
async def test_a_git_verb_on_a_project_you_cannot_see_is_a_404(client, verb):
    resp = await client.post(f"/v1/projects/demo/{verb}", headers=_as(MEMBER_B),
                             json={"name": "x", "message": "x"})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_a_branch_then_a_commit_goes_through_the_door(client):
    await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))
    resp = await client.post("/v1/projects/demo/branch", headers=_as(MEMBER_A),
                             json={"name": "trabajo"})
    assert resp.status == 200
    assert (await resp.json())["branch"] == "alfred/trabajo"

    path = client.broker.workspace.path_for(MEMBER_A, "demo")
    (path / "nuevo.txt").write_text("hola\n", encoding="utf-8")
    resp = await client.post("/v1/projects/demo/commit", headers=_as(MEMBER_A),
                             json={"message": "Add nuevo.txt"})
    assert resp.status == 200, await resp.json()
    assert (await resp.json())["dirty"] is False


@pytest.mark.asyncio
async def test_the_floor_the_commit_obeys_is_the_registry_s(client):
    """Not a constant in this process, and not something the agent sends: the
    registry is asked on every commit, so changing a project's protected paths
    takes effect on the next one rather than on the next deploy."""
    client.registry["projects"]["demo"]["project"]["protected_paths"] = ["*.pem", "users.json"]
    await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))
    await client.post("/v1/projects/demo/branch", headers=_as(MEMBER_A), json={"name": "trabajo"})

    path = client.broker.workspace.path_for(MEMBER_A, "demo")
    (path / "deploy").mkdir(parents=True, exist_ok=True)
    (path / "deploy" / "api.pem").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    resp = await client.post("/v1/projects/demo/commit", headers=_as(MEMBER_A),
                             json={"message": "Add the key"})
    assert resp.status == 403
    assert "api.pem" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_a_commit_on_the_trunk_is_refused_at_the_door_too(client):
    await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))
    path = client.broker.workspace.path_for(MEMBER_A, "demo")
    (path / "nuevo.txt").write_text("hola\n", encoding="utf-8")
    resp = await client.post("/v1/projects/demo/commit", headers=_as(MEMBER_A),
                             json={"message": "Straight onto main"})
    assert resp.status == 403


@pytest.mark.asyncio
async def test_the_whole_loop_a_task_actually_walks(client, tmp_path):
    """checkout → branch → edit → commit → push, through the door, once.

    Each verb has its own tests above; this is the one that would have caught a
    step that works alone and not in sequence — which is the shape most of
    these failures take.
    """
    import subprocess

    # A remote that can be pushed to, standing in for the project's real one.
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare",
                    client.registry["projects"]["demo"]["project"]["git_url"], str(bare)],
                   check=True, capture_output=True)
    client.registry["projects"]["demo"]["project"]["git_url"] = str(bare)
    client.registry["projects"]["demo"]["project"]["protected_paths"] = ["*.pem", "users.json"]

    out = await (await client.post("/v1/projects/demo/checkout", headers=_as(MEMBER_A))).json()
    assert out["action"] == "cloned"
    assert out["project"]["protected_paths"] == ["*.pem", "users.json"]

    out = await (await client.post("/v1/projects/demo/branch", headers=_as(MEMBER_A),
                                   json={"name": "arregla-la-grilla"})).json()
    assert out["branch"] == "alfred/arregla-la-grilla"

    path = client.broker.workspace.path_for(MEMBER_A, "demo")
    (path / "grid.css").write_text(".grid { display: grid }\n", encoding="utf-8")

    out = await (await client.post("/v1/projects/demo/commit", headers=_as(MEMBER_A),
                                   json={"message": "Fix the camera grid on narrow phones"})).json()
    assert out["dirty"] is False, out

    out = await (await client.post("/v1/projects/demo/push", headers=_as(MEMBER_A))).json()
    assert out["action"] == "pushed", out

    # The branch is on the remote, with the file, authored and co-authored.
    log = subprocess.run(["git", "log", "-1", "--format=%an <%ae>|%B", "alfred/arregla-la-grilla"],
                         cwd=bare, capture_output=True, text=True).stdout
    assert log.startswith(f"{DISPLAY_NAMES[USERS[MEMBER_A]]} <{USERS[MEMBER_A]}@example.com>|"), log
    assert "Fix the camera grid on narrow phones" in log
    assert "Co-authored-by: Alfred (user2)" in log, log
    files = subprocess.run(["git", "ls-tree", "--name-only", "alfred/arregla-la-grilla"],
                           cwd=bare, capture_output=True, text=True).stdout
    assert "grid.css" in files

    # And main is untouched: that is the whole point.
    main = subprocess.run(["git", "log", "--oneline", "main"], cwd=bare,
                          capture_output=True, text=True).stdout
    assert main.count("\n") == 1, "the agent's work must not have reached main"


# --- which registry it talks to ---------------------------------------------

@pytest.mark.parametrize("env,expected", [
    ({"HOMECORE_URL": "http://home-core:21001"}, "http://home-core:21001"),
    # Trimmed, because the manifest interpolates this and a stray newline in
    # config would otherwise become part of every registry URL.
    ({"HOMECORE_URL": "  http://home-core:21001\n"}, "http://home-core:21001"),
    # Unset is empty, not a guess. Upstream's copy defaulted to a hostname from
    # the household it was written in; a wrong address fails on the network,
    # which reads like the registry being down, while an empty one fails on the
    # URL and `build_from_env` says so at startup.
    ({}, ""),
])
def test_the_registry_url_is_read_from_the_name_the_compose_sets(
        monkeypatch, env, expected):
    """HOMECORE_URL, and nothing else.

    Upstream read HOMEWEB_URL here while its compose set HOMECORE_URL, so the
    configured value was ignored and a hardcoded default was silently in force.
    Both halves of that are absent in this package -- the rename landed at
    extraction, and `deploy/manifest.yml` supplies HOMECORE_URL from
    HOMECORE_CONTAINER_URL. HOMEWEB_URL is deliberately *not* accepted as an
    alias: nothing here has ever set it, so honouring it would only widen the
    surface --check-contract has to reason about.
    """
    from nanobot.code_broker.service import registry_url

    for k in ("HOMECORE_URL", "HOMEWEB_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert registry_url() == expected


def test_a_leftover_homeweb_url_is_not_honoured(monkeypatch):
    """The old name is ignored rather than aliased.

    CLAUDE.md aliases old names on read where a stored value would otherwise be
    orphaned. Nothing is stored here: this is read from the environment the
    deploy writes, on every start. An alias would mean a name the manifest does
    not export can still decide where the broker sends the household's project
    credentials, and the contract check would not see it.
    """
    from nanobot.code_broker.service import registry_url

    monkeypatch.delenv("HOMECORE_URL", raising=False)
    monkeypatch.setenv("HOMEWEB_URL", "http://somewhere-else:8443")
    assert registry_url() == ""


def test_an_unset_registry_says_so_at_startup(monkeypatch, caplog):
    """The log line is the fix.

    Upstream found this bug by reading /health and noticing the broker had
    settled on a registry nobody configured. What made it invisible for as long
    as it was is that startup never said which one it picked -- so this asserts
    the sentence, not the value.
    """
    from nanobot.code_broker import service as svc

    for k in ("HOMECORE_URL", "HOMEWEB_URL", "CODE_BROKER_USERS",
              "PROJECTS_BROKER_TOKEN", "CODE_BROKER_SECRET"):
        monkeypatch.delenv(k, raising=False)
    said = []
    monkeypatch.setattr(svc.logger, "warning",
                        lambda m, *a, **k: said.append(m.format(*a, **k)))
    monkeypatch.setattr(svc.logger, "info",
                        lambda m, *a, **k: said.append(m.format(*a, **k)))
    svc.build_from_env()
    assert any("HOMECORE_URL unset" in s for s in said), said


@pytest.mark.asyncio
async def test_an_unknown_path_says_not_found(client):
    """aiohttp raises HTTPNotFound to *mean* 404, and the error middleware's
    catch-all swallowed it: every unrouted URL answered 500 with "internal
    error: HTTPNotFound", which reads as the broker having broken rather than
    as there being no such verb.

    Found by deleting two routes and checking they 404. They did not."""
    for path in ("/v1/nonsense", "/nope", "/v1/projects/demo/nothing"):
        resp = await client.get(path, headers=_as(MEMBER_A))
        assert resp.status == 404, f"{path} answered {resp.status}"


@pytest.mark.asyncio
async def test_a_real_bug_still_says_500(client, monkeypatch):
    """The catch-all is still the catch-all. An HTTPException means a status on
    purpose; anything else is a fault and has to keep saying so."""
    # Patched on the instance the route already bound, not on the class: the
    # router holds the bound method from registration, so a class-level patch
    # after create_app never takes and the test passes for the wrong reason.
    def _boom():
        raise RuntimeError("something actually broke")
    monkeypatch.setattr(client.broker.workspace, "require_marker", _boom)
    resp = await client.get("/v1/projects/demo/status", headers=_as(MEMBER_A))
    assert resp.status == 500, resp.status
