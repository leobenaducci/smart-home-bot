"""What the broker must refuse, and what it must never leave behind.

The agent can read its whole container: `sandbox: ""`, `restrictToWorkspace:
false`, 29 credentials in the environment. So "the key is not in the checkout"
is not a tidiness preference — it is the only thing making a per-project key
mean anything, and it is what most of this file is about.

The rest is the workspace boundary. A slug reaches `path_for` from a registry
that already constrains it and a user from a token the broker already verified,
so anything failing those checks is a bug or an attack; both are worth an error
rather than a quietly sanitised path.
"""
import os
import subprocess
from pathlib import Path

import pytest

from nanobot.code_broker.workspace import (
    MARKER,
    BrokerError,
    Credential,
    Workspace,
    deploy,
    _refuses,
    _scrub,
    branch,
    checkout,
    commit,
    merge,
    pull,
    push,
    status,
)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "nanobot-code-workspace"
    root.mkdir()
    (root / MARKER).write_text("test workspace\n", encoding="utf-8")
    return Workspace(root)


@pytest.fixture
def origin(tmp_path):
    """A real repository to clone, so the git path under test is the real one."""
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


# --- the workspace boundary -------------------------------------------------

def test_a_directory_without_the_marker_is_not_a_workspace(tmp_path):
    """CODE_WORKSPACE_DIR pointing at a home directory, or at a mount that did
    not appear, must be an error rather than a repository landing in it."""
    bare = Workspace(tmp_path / "somebodys-home")
    (tmp_path / "somebodys-home").mkdir()

    with pytest.raises(BrokerError) as exc:
        bare.require_marker()
    assert MARKER in str(exc.value)
    assert exc.value.status == 503


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "", "A", "x", "user2/../user3"])
def test_a_path_is_refused_rather_than_sanitised(workspace, bad):
    with pytest.raises(BrokerError):
        workspace.path_for(bad, "proj")
    with pytest.raises(BrokerError):
        workspace.path_for("user2", bad)


def test_the_path_is_user_then_project(workspace):
    path = workspace.path_for("user2", "homecameras")
    assert path == (workspace.root / "user2" / "homecameras").resolve()


def test_one_users_path_never_lands_in_anothers(workspace):
    a = workspace.path_for("user2", "shared")
    b = workspace.path_for("user3", "shared")
    assert a != b and "user2" in str(a) and "user3" in str(b)


# --- the credential ---------------------------------------------------------

def test_an_ssh_key_exists_only_inside_the_block():
    cred = Credential("ssh_key", "-----BEGIN OPENSSH PRIVATE KEY-----\nsecreto\n")
    with cred as c:
        key = Path(c.env()["GIT_SSH_COMMAND"].split("-i ")[1].split()[0])
        assert key.is_file()
        assert "secreto" in key.read_text()
        assert oct(key.stat().st_mode)[-3:] == "600", "readable by anyone is not a key"
        assert oct(key.parent.stat().st_mode)[-3:] == "700"
    assert not key.exists(), "and it is gone on the way out"


def test_the_key_is_removed_even_when_the_operation_raises():
    cred = Credential("ssh_key", "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n")
    try:
        with cred as c:
            key = Path(c.env()["GIT_SSH_COMMAND"].split("-i ")[1].split()[0])
            raise RuntimeError("the remote refused")
    except RuntimeError:
        pass
    assert not key.exists()


def test_a_token_never_becomes_a_file():
    with Credential("token", "pat-123456") as cred:
        assert cred.env() == {}, "nothing to point at"
        args = cred.git_args()
    assert any("Authorization: Bearer pat-123456" in a for a in args)
    assert not any(a.startswith("http") and "pat-123456" in a and "://" in a for a in args), \
        "a token in the URL is what ends up in .git/config"


def test_no_credential_is_no_arguments():
    with Credential("none", "") as cred:
        assert cred.git_args() == [] and cred.env() == {}


# --- the checkout -----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_clone_lands_where_it_should_and_says_what_it_did(workspace, origin):
    out = await checkout(workspace, "user2", "demo", str(origin), Credential("none", ""))

    assert out["action"] == "cloned"
    assert out["path"] == str(workspace.root / "user2" / "demo")
    assert out["branch"] == "main"
    assert out["dirty"] is False
    assert (workspace.root / "user2" / "demo" / "README.md").is_file()


@pytest.mark.asyncio
async def test_asking_twice_fetches_instead_of_failing(workspace, origin):
    await checkout(workspace, "user2", "demo", str(origin), Credential("none", ""))
    out = await checkout(workspace, "user2", "demo", str(origin), Credential("none", ""))

    assert out["action"] == "fetched"


@pytest.mark.asyncio
async def test_a_refresh_does_not_disturb_work_in_progress(workspace, origin):
    """Fetch, not pull. Somebody asking for a refresh must not rearrange a tree
    the agent is halfway through editing."""
    await checkout(workspace, "user2", "demo", str(origin), Credential("none", ""))
    scratch = workspace.root / "user2" / "demo" / "README.md"
    scratch.write_text("a medio escribir\n", encoding="utf-8")

    out = await checkout(workspace, "user2", "demo", str(origin), Credential("none", ""))

    assert scratch.read_text() == "a medio escribir\n"
    assert out["dirty"] is True
    assert out["changed"] == ["README.md"]


@pytest.mark.asyncio
async def test_the_checkout_carries_no_credential(workspace, origin):
    """The agent can read this whole tree. If the key were in it, having put
    the key behind the broker would have bought nothing."""
    await checkout(workspace, "user2", "demo", str(origin),
                   Credential("token", "pat-do-not-leak"))

    tree = workspace.root / "user2" / "demo"
    config = (tree / ".git" / "config").read_text()
    assert "pat-do-not-leak" not in config
    assert "extraHeader" not in config, "a -c flag must not persist as config"

    found = [p for p in tree.rglob("*") if p.is_file()
             and "pat-do-not-leak" in p.read_bytes().decode("utf-8", "replace")]
    assert not found, found


@pytest.mark.asyncio
async def test_a_clone_into_an_unmarked_workspace_never_starts(tmp_path, origin):
    unmarked = Workspace(tmp_path / "nope")
    (tmp_path / "nope").mkdir()

    with pytest.raises(BrokerError):
        await checkout(unmarked, "user2", "demo", str(origin), Credential("none", ""))
    assert not (tmp_path / "nope" / "user2").exists(), "and left nothing behind"


@pytest.mark.asyncio
async def test_a_remote_that_will_not_answer_is_a_502_not_a_traceback(workspace, tmp_path):
    with pytest.raises(BrokerError) as exc:
        await checkout(workspace, "user2", "demo", str(tmp_path / "no-such-repo"),
                       Credential("none", ""))
    assert exc.value.status == 502


@pytest.mark.asyncio
async def test_status_before_a_checkout_is_a_404(workspace):
    with pytest.raises(BrokerError) as exc:
        await status(workspace, "user2", "demo")
    assert exc.value.status == 404


@pytest.mark.asyncio
async def test_status_reports_the_branch_and_head(workspace, origin):
    await checkout(workspace, "user2", "demo", str(origin), Credential("none", ""))
    out = await status(workspace, "user2", "demo")

    assert out["branch"] == "main"
    assert len(out["head"]) >= 7


# --- errors on the way back -------------------------------------------------

def test_an_error_does_not_echo_the_credential_it_used():
    """An auth failure frequently repeats what it was given, and that error is
    on its way to the agent."""
    assert "pat-123456" not in _scrub("fatal: rejected token pat-123456", "pat-123456")
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2EAAAA\n"
    assert "AAAAB3NzaC1yc2EAAAA" not in _scrub(f"bad key: {key}", key)


def test_scrubbing_leaves_a_short_string_alone():
    """`***`-ing every occurrence of a three-character secret would redact the
    message instead of the secret."""
    assert _scrub("fatal: repository 'abc' not found", "abc") == \
        "fatal: repository 'abc' not found"


# --- git the agent may run, and the two lines it may not cross ---------------
#
# `commit` and `push` are where the house's one hard rule stops being prose. It
# is written in ALFRED_PROGRAMADOR.md and repeated in the skill, but prose is
# what an agent talks itself past, so both refusals are enforced here where no
# prompt reaches: work never lands on a trunk branch, and a protected path is
# never committed.

@pytest.fixture
def bare(tmp_path, origin):
    """Somewhere a push can actually land, with a history already in it.

    Seeded from `origin`: an empty bare repo has no branch, so a checkout of it
    sits on an unborn HEAD and every test below would be exercising that
    instead of the thing it names.
    """
    repo = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(repo)],
                   check=True, capture_output=True)
    return repo


async def _clone(workspace, origin, user="user2", slug="proj"):
    """A checkout with *no* git identity configured, deliberately.

    The broker's container configures none either, and `git commit` without one
    does not warn, it fails — so a helpful `git config` here would have hidden
    that until the first real commit.
    """
    await checkout(workspace, user, slug, str(origin), Credential("none", ""))
    return workspace.path_for(user, slug)


def _write(path, name, text="cambio\n"):
    f = path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


# --- which paths the floor catches ------------------------------------------

@pytest.mark.parametrize("path,pattern", [
    ("users.json", "users.json"),
    ("local/users.json", "users.json"),
    ("deploy/certs/api.pem", "*.pem"),
    ("services/api/.env", ".env"),
    (".git/config", ".git/config"),
    ("infra/id_rsa", "id_rsa*"),
])
def test_a_protected_path_is_caught_wherever_it_sits(path, pattern):
    assert _refuses([path], [pattern]) == [path]


def test_an_ordinary_file_is_not_caught():
    assert _refuses(["src/app.py", "README.md"], ["*.pem", "users.json", ".env"]) == []


# --- branch -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_branch_is_created_and_becomes_the_current_one(workspace, origin):
    await _clone(workspace, origin)
    out = await branch(workspace, "user2", "proj", "arreglo-de-la-grilla")
    assert out["action"] == "created"
    assert out["branch"] == "alfred/arreglo-de-la-grilla", "the house prefix is added"


@pytest.mark.asyncio
async def test_asking_for_a_branch_that_exists_moves_onto_it(workspace, origin):
    await _clone(workspace, origin)
    await branch(workspace, "user2", "proj", "otra")
    await branch(workspace, "user2", "proj", "tercera")
    out = await branch(workspace, "user2", "proj", "otra")
    assert out["action"] == "switched" and out["branch"] == "alfred/otra"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["main", "master", "trunk", "develop", "feature/main"])
async def test_a_trunk_branch_is_never_created(workspace, origin, name):
    await _clone(workspace, origin)
    with pytest.raises(BrokerError) as exc:
        await branch(workspace, "user2", "proj", name)
    assert exc.value.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["--upload-pack=x", "a..b", "", "con espacio", "/leading"])
async def test_a_branch_name_that_is_not_one_is_refused(workspace, origin, name):
    await _clone(workspace, origin)
    with pytest.raises(BrokerError) as exc:
        await branch(workspace, "user2", "proj", name)
    assert exc.value.status == 400


# --- commit -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_commit_on_a_branch_lands(workspace, origin):
    path = await _clone(workspace, origin)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    out = await commit(workspace, "user2", "proj", "Add the thing", protected=["*.pem"])
    assert out["action"] == "committed" and out["dirty"] is False


@pytest.mark.asyncio
async def test_a_commit_on_main_is_refused_before_anything_is_staged(workspace, origin):
    path = await _clone(workspace, origin)
    _write(path, "src/app.py")
    with pytest.raises(BrokerError) as exc:
        await commit(workspace, "user2", "proj", "Add the thing", protected=[])
    assert exc.value.status == 403
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=path,
                            capture_output=True, text=True).stdout
    assert staged == "", "a refused commit must not leave the index staged"


@pytest.mark.asyncio
async def test_a_protected_path_stops_the_commit_and_leaves_nothing_staged(workspace, origin):
    path = await _clone(workspace, origin)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    _write(path, "deploy/certs/api.pem", "-----BEGIN PRIVATE KEY-----\n")
    with pytest.raises(BrokerError) as exc:
        await commit(workspace, "user2", "proj", "Add the thing",
                     protected=["users.json", "*.pem"])
    assert exc.value.status == 403
    assert "api.pem" in str(exc.value)
    head = subprocess.run(["git", "log", "--oneline"], cwd=path,
                          capture_output=True, text=True).stdout
    assert head.count("\n") == 1, "nothing may have been committed"
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=path,
                            capture_output=True, text=True).stdout
    assert staged == "", "the index is reset when the floor refuses"


@pytest.mark.asyncio
async def test_a_commit_needs_a_message_and_something_to_say_it_about(workspace, origin):
    path = await _clone(workspace, origin)
    await branch(workspace, "user2", "proj", "trabajo")
    with pytest.raises(BrokerError) as exc:
        await commit(workspace, "user2", "proj", "   ")
    assert exc.value.status == 400
    with pytest.raises(BrokerError) as exc:
        await commit(workspace, "user2", "proj", "Nada cambió")
    assert exc.value.status == 400


# --- push -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_branch_is_published_and_the_remote_has_it(workspace, bare):
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    out = await push(workspace, "user2", "proj", Credential("none", ""))
    assert out["action"] == "pushed" and out["branch"] == "alfred/trabajo"
    refs = subprocess.run(["git", "branch", "--list"], cwd=bare,
                          capture_output=True, text=True).stdout
    assert "alfred/trabajo" in refs


@pytest.mark.asyncio
async def test_pushing_a_trunk_branch_is_refused_because_that_is_merging(workspace, bare):
    await _clone(workspace, bare)
    with pytest.raises(BrokerError) as exc:
        await push(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 403
    assert "merging" in str(exc.value)


@pytest.mark.asyncio
async def test_uncommitted_work_is_not_pushed_silently(workspace, bare):
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    with pytest.raises(BrokerError) as exc:
        await push(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 400


# --- merge ------------------------------------------------------------------
# `push` refuses a trunk branch because pushing to main *is* merging. `merge` is
# that same act done on purpose, once the person has approved the work -- so
# what matters here is that it lands, and that it still stops dead on the three
# things that are somebody's decision rather than a missing keystroke.

@pytest.mark.asyncio
async def test_an_approved_branch_lands_on_the_trunk(workspace, bare):
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    await push(workspace, "user2", "proj", Credential("none", ""))

    out = await merge(workspace, "user2", "proj", Credential("none", ""))
    assert out["action"] == "merged"
    assert out["branch"] == "alfred/trabajo"

    # The remote's trunk is what the person will clone, so that is what is
    # asserted -- not the local checkout, which would pass with a push that
    # never left the machine.
    trunk = out["trunk"]
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", trunk],
                            cwd=bare, capture_output=True, text=True).stdout
    assert "src/app.py" in listed


@pytest.mark.asyncio
async def test_the_merge_is_one_commit_that_names_the_branch(workspace, bare):
    """`--no-ff`: a year later the log has to say the change arrived as a piece."""
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    out = await merge(workspace, "user2", "proj", Credential("none", ""))

    subject = subprocess.run(["git", "log", "-1", "--format=%s", out["trunk"]],
                             cwd=bare, capture_output=True, text=True).stdout
    assert "Merge branch 'alfred/trabajo'" in subject


@pytest.mark.asyncio
async def test_a_branch_already_on_the_trunk_is_not_reported_as_merged(
        workspace, bare):
    """"Already there" is a different fact from "I put it there".

    `git merge` exits 0 either way. Reporting the second as the first is how a
    merge somebody else had already done -- by hand, in a shell -- came back as
    «merge listo», with nothing for the person to notice.
    """
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    first = await merge(workspace, "user2", "proj", Credential("none", ""))
    assert first["action"] == "merged"

    # Straight back onto the branch and round again: nothing has changed since,
    # so there is nothing to merge.
    subprocess.run(["git", "checkout", "-q", "alfred/trabajo"], cwd=path,
                   check=True, capture_output=True)
    again = await merge(workspace, "user2", "proj", Credential("none", ""))
    assert again["action"] == "already-merged"
    assert "already contained" in again["note"]


@pytest.mark.asyncio
async def test_merging_the_trunk_into_itself_is_refused(workspace, bare):
    await _clone(workspace, bare)
    with pytest.raises(BrokerError) as exc:
        await merge(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_uncommitted_work_is_not_merged(workspace, bare):
    """What is merged has to be what was reviewed."""
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    with pytest.raises(BrokerError) as exc:
        await merge(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_a_conflict_stops_and_changes_nothing(workspace, bare):
    """A conflict is a person's to resolve, and the branch must survive it."""
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    (path / "shared.txt").write_text("de la rama\n", encoding="utf-8")
    await commit(workspace, "user2", "proj", "Branch writes it")

    # The same file, differently, straight onto the remote's trunk.
    trunk = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"],
                           cwd=bare, capture_output=True, text=True).stdout.strip()
    other = path.parent / "otro"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True,
                   capture_output=True)
    (other / "shared.txt").write_text("del tronco\n", encoding="utf-8")
    for args in (["add", "-A"],
                 ["-c", "user.email=t@e", "-c", "user.name=T", "commit", "-qm", "Trunk writes it"],
                 ["push", "-q", "origin", trunk]):
        subprocess.run(["git", *args], cwd=other, check=True, capture_output=True)

    with pytest.raises(BrokerError) as exc:
        await merge(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 409

    # Back on the branch, nothing half-merged, and the work still there: a
    # checkout left mid-merge is the worst thing to hand back to an agent that
    # will carry on issuing git verbs into it.
    described = await status(workspace, "user2", "proj")
    assert described["branch"] == "alfred/trabajo"
    assert not described["dirty"]
    assert (path / "shared.txt").read_text(encoding="utf-8") == "de la rama\n"


# --- pull -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pull_fast_forwards(workspace, bare, tmp_path):
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    await push(workspace, "user2", "proj", Credential("none", ""))
    # Somebody else moves the branch on.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    for k, v in (("user.email", "b@b"), ("user.name", "B")):
        subprocess.run(["git", "config", k, v], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "switch", "-q", "alfred/trabajo"], cwd=other, check=True, capture_output=True)
    (other / "otro.txt").write_text("mas\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q"], cwd=other, check=True, capture_output=True)

    out = await pull(workspace, "user2", "proj", Credential("none", ""))
    assert out["action"] == "pulled"
    assert (path / "otro.txt").is_file()


@pytest.mark.asyncio
async def test_a_pull_will_not_rearrange_uncommitted_work(workspace, bare):
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    with pytest.raises(BrokerError) as exc:
        await pull(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 400


@pytest.mark.asyncio
async def test_a_diverged_branch_is_a_person_s_problem_not_a_merge(workspace, bare, tmp_path):
    path = await _clone(workspace, bare)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    await push(workspace, "user2", "proj", Credential("none", ""))
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    for k, v in (("user.email", "b@b"), ("user.name", "B")):
        subprocess.run(["git", "config", k, v], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "switch", "-q", "alfred/trabajo"], cwd=other, check=True, capture_output=True)
    (other / "suyo.txt").write_text("suyo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "theirs"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "push", "-q"], cwd=other, check=True, capture_output=True)
    # And the agent commits something of its own on top of the old head.
    _write(path, "mio.txt", "mio\n")
    await commit(workspace, "user2", "proj", "Mine")

    with pytest.raises(BrokerError) as exc:
        await pull(workspace, "user2", "proj", Credential("none", ""))
    assert exc.value.status == 409


@pytest.mark.asyncio
async def test_a_commit_works_without_any_git_identity_configured(workspace, origin, monkeypatch):
    """Nothing configures git in the broker's container, so the identity has to
    come from the invocation. Without it the first commit anybody asked for
    would have died on «Please tell me who you are» — which this machine hides,
    because a developer running the tests has a global identity the container
    does not. So both config layers are pointed at nothing here.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    path = await _clone(workspace, origin)
    bare_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    assert subprocess.run(["git", "config", "user.email"], cwd=path, env=bare_env,
                          capture_output=True).returncode != 0, "no identity, as in production"

    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")

    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=path,
                            env=bare_env, capture_output=True, text=True).stdout.strip()
    assert author == "Alfred (nanobot) <alfred@example.com>", author  # no name sent


@pytest.mark.asyncio
async def test_a_merge_works_without_any_git_identity_configured(
        workspace, bare, monkeypatch):
    """`--no-ff` writes a commit, so the merge needs an identity too.

    This is the failure the commit test above exists to catch, in the one verb
    that did not carry the fix: in the container git refused the merge commit
    with "Committer identity unknown", and the broker reported that as a merge
    *conflict* -- so a clean fast-forward came back as «hay conflicto», about
    two histories that had none. On a developer's machine it passed, because
    the global identity the container has not is right there.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    path = await _clone(workspace, bare)
    bare_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull}
    assert subprocess.run(["git", "config", "user.email"], cwd=path, env=bare_env,
                          capture_output=True).returncode != 0, "no identity, as in production"

    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing")
    out = await merge(workspace, "user2", "proj", Credential("none", ""))
    assert out["action"] == "merged"

    subject = subprocess.run(["git", "log", "-1", "--format=%s", out["trunk"]],
                             cwd=bare, capture_output=True, text=True).stdout
    assert "Merge branch 'alfred/trabajo'" in subject


@pytest.mark.asyncio
async def test_the_person_owns_the_commit_and_the_agent_is_named_beside_them(workspace, origin):
    """`git log` is read by people, so the author is the name the house uses.
    Alfred stays on the commit as co-author: ALFRED_PROGRAMADOR.md is right
    that the log must keep distinguishing his work from a person's, and naming
    him second does that without every commit claiming somebody typed it."""
    path = await _clone(workspace, origin)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing",
                 asked_by="user2", author="Member Two")
    log = subprocess.run(["git", "log", "-1", "--format=%an <%ae>|%B"], cwd=path,
                         capture_output=True, text=True).stdout
    assert log.startswith("Member Two <user2@example.com>|"), log
    assert "Co-authored-by: Alfred (user2) <alfred@example.com>" in log, log


@pytest.mark.asyncio
async def test_without_a_name_the_commit_says_it_was_the_agent(workspace, origin):
    """A registry that sent no name must not produce a commit with somebody's
    name on it. An obviously-automated author is the honest fallback."""
    path = await _clone(workspace, origin)
    await branch(workspace, "user2", "proj", "trabajo")
    _write(path, "src/app.py")
    await commit(workspace, "user2", "proj", "Add the thing", asked_by="user2")
    author = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=path,
                            capture_output=True, text=True).stdout.strip()
    assert author == "Alfred (nanobot)", author


@pytest.mark.asyncio
async def test_a_name_that_already_carries_the_prefix_is_not_given_it_twice(workspace, origin):
    await _clone(workspace, origin)
    out = await branch(workspace, "user2", "proj", "alfred/ya-tiene")
    assert out["branch"] == "alfred/ya-tiene"


# --- running a deploy script ------------------------------------------------
#
# The whole point of the verb is that the agent can cause a deploy without
# holding the credential. So what is pinned is: the secret reaches the script,
# the secret does not come back, and "run the deploy" cannot become "run
# anything".

@pytest.fixture
def checked_out(workspace, origin):
    """A checkout with a deploy script in it, without going through git."""
    root = workspace.path_for("user1", "proj")
    (root / ".git").mkdir(parents=True)
    return root


@pytest.mark.asyncio
async def test_the_script_gets_the_credential_and_the_output_does_not(
        workspace, checked_out):
    (checked_out / "deploy.sh").write_text(
        'echo "user=$DEPLOY_USER"\necho "pass=$DEPLOY_PASSWORD"\n',
        encoding="utf-8")
    got = await deploy(workspace, "user1", "proj", {"deploy_path": "deploy.sh"},
                       Credential("basic", "hunter2", "deploy@example.com"))
    assert got["ok"] and got["exit_code"] == 0, got
    # The account is not a secret and is useful in a log.
    assert "deploy@example.com" in got["stdout"], got
    # The password is, and the script printed it deliberately -- which is what
    # `set -x` does by accident in half the deploy scripts ever written.
    assert "hunter2" not in got["stdout"], got
    assert "***" in got["stdout"], got


@pytest.mark.asyncio
async def test_even_a_short_password_is_taken_back_out(workspace, checked_out):
    """`_scrub` ignores six characters or fewer, and people choose short
    passwords. The deploy path must not inherit that floor: it was written for
    keeping a key out of a git error, where replacing a short string would
    mangle unrelated output."""
    (checked_out / "deploy.sh").write_text('echo "p=$DEPLOY_PASSWORD"\n',
                                           encoding="utf-8")
    got = await deploy(workspace, "user1", "proj", {"deploy_path": "deploy.sh"},
                       Credential("basic", "abc12", "u"))
    assert "abc12" not in got["stdout"], got


@pytest.mark.asyncio
async def test_a_failing_script_is_reported_as_failing(workspace, checked_out):
    (checked_out / "deploy.sh").write_text("echo nope >&2\nexit 3\n",
                                           encoding="utf-8")
    got = await deploy(workspace, "user1", "proj", {"deploy_path": "deploy.sh"},
                       Credential("none", "", ""))
    assert got["ok"] is False and got["exit_code"] == 3, got
    assert "nope" in got["stderr"], got


@pytest.mark.asyncio
async def test_the_stored_script_runs_and_never_lands_in_the_repo(workspace,
                                                                  checked_out):
    """A project's deploy script is content, not a path into its own source.

    Writing it into the checkout would put it in every diff and status, and a
    file the deploy writes into the tree it is deploying is one somebody
    eventually commits by accident."""
    got = await deploy(workspace, "user1", "proj",
                       {"deploy_script": 'echo "ran as $DEPLOY_USER"\n'},
                       Credential("basic", "hunter2", "deploy@example.com"))
    assert got["ok"], got
    assert "deploy@example.com" in got["stdout"], got
    # Nothing left behind in the project.
    assert sorted(p.name for p in checked_out.iterdir()) == [".git"], \
        sorted(p.name for p in checked_out.iterdir())


@pytest.mark.asyncio
async def test_the_stored_script_wins_over_a_path(workspace, checked_out):
    (checked_out / "deploy.sh").write_text("echo from-the-repo\n", encoding="utf-8")
    got = await deploy(workspace, "user1", "proj",
                       {"deploy_script": "echo from-the-setting\n",
                        "deploy_path": "deploy.sh"},
                       Credential("none", "", ""))
    assert "from-the-setting" in got["stdout"], got
    assert "from-the-repo" not in got["stdout"], got


@pytest.mark.asyncio
async def test_a_script_outside_the_checkout_is_refused(workspace, checked_out,
                                                        tmp_path):
    (tmp_path / "evil.sh").write_text("echo $DEPLOY_PASSWORD\n", encoding="utf-8")
    with pytest.raises(BrokerError):
        await deploy(workspace, "user1", "proj",
                     {"deploy_path": "../../../evil.sh"},
                     Credential("basic", "hunter2", "u"))


@pytest.mark.asyncio
async def test_no_script_configured_is_a_refusal_not_a_crash(workspace,
                                                             checked_out):
    with pytest.raises(BrokerError):
        await deploy(workspace, "user1", "proj", {"deploy_path": ""},
                     Credential("none", "", ""))


@pytest.mark.asyncio
async def test_a_project_that_is_not_checked_out_says_so(workspace):
    with pytest.raises(BrokerError):
        await deploy(workspace, "user1", "absent", {"deploy_path": "deploy.sh"},
                     Credential("none", "", ""))
