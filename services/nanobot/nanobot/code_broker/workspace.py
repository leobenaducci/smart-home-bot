"""Checkouts, and the git that fills them.

Every rule the agent must not talk its way past lives on this side of an HTTP
boundary: which directory a project may land in, whether the workspace is the
one the deploy created, and the fact that a credential exists for the length of
one subprocess and nowhere else.

Three properties are worth naming because each was a way to lose a secret:

* **Nothing is ever interpolated into a shell.** Every git call is an argv
  list. A project's URL comes from a registry an admin fills in, and validating
  it there is defence in depth rather than the only defence.
* **The checkout never learns the credential.** An SSH key reaches git through
  `GIT_SSH_COMMAND` pointing at a file outside the tree; an HTTPS token reaches
  it through `http.extraHeader` on the command line of that one invocation.
  Neither is written into `.git/config`, so a checkout copied elsewhere — or
  read by the agent, which can read the whole tree — carries nothing.
* **The remote in the tree is the clean URL.** `git clone https://user:token@…`
  would persist the token in `.git/config` and print it in `git remote -v`,
  which is the classic way this leaks.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from loguru import logger

# Written by build-multiuser.sh at the root of the workspace. Its absence means
# CODE_WORKSPACE_DIR points somewhere nobody intended — a home directory, or a
# mount that did not appear — and cloning into that is worse than refusing.
MARKER = ".nanobot-code-workspace"

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
# Branch names are looser than slugs — `feature/x`, `fix-123`, `v2.1` are all
# real — but never a leading dash (git would read it as an option) and never
# `..` (a refspec traversal).
_SAFE_BRANCH = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_GIT_TIMEOUT_S = 300

# Who the commits say they are. Nothing in this container configures git, and
# `git commit` with no identity does not warn — it fails, and it would have
# failed on the very first one. Set on the invocation rather than written into
# the checkout's config: the same reason the credential is, and it keeps a
# checkout that somebody copies from carrying this process's idea of itself.
#
# The *author* is the person who asked, by the name the house calls them —
# `git log` is read by people, and `user1` is an access-control identifier
# that means nothing a year later. The registry sends that name, because the
# mapping to it lives there.
#
# Alfred goes on the co-author line rather than off the commit.
# ALFRED_PROGRAMADOR.md is right that the log has to keep distinguishing his
# work from a person's; naming him second does that just as well as naming him
# first, and it stops every commit claiming a person typed it.
_AGENT_NAME = os.environ.get("CODE_COMMIT_NAME", "Alfred (nanobot)")
_AGENT_EMAIL = os.environ.get("CODE_COMMIT_EMAIL", "alfred@example.com")

# Every branch he opens is his, and says so in the remote's branch list.
# ALFRED_PROGRAMADOR.md fixes the shape as `alfred/<task>`; a name that arrives
# without it gets it, rather than being refused over a prefix.
_BRANCH_PREFIX = "alfred/"


class BrokerError(Exception):
    """Something the caller did wrong, or a remote that would not answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class GitResult:
    ok: bool
    stdout: str
    stderr: str


class Workspace:
    """The directory tree of checkouts, one subdirectory per user."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def require_marker(self) -> None:
        if not (self.root / MARKER).is_file():
            raise BrokerError(
                f"{self.root} is not a code workspace: {MARKER} is missing. "
                "It is created by build-multiuser.sh; check CODE_WORKSPACE_DIR.",
                status=503,
            )

    def path_for(self, user: str, slug: str) -> Path:
        """`<root>/<user>/<slug>`, or an error.

        Both halves are pattern-checked rather than sanitised. A slug arrives
        from the registry, which constrains it already, and a user from a token
        the broker verified — so anything failing here is a bug or an attack,
        and quietly rewriting `../` into something safe would hide both.
        """
        if not _SAFE_NAME.match(user or ""):
            raise BrokerError(f"invalid user {user!r}")
        if not _SAFE_NAME.match(slug or ""):
            raise BrokerError(f"invalid project {slug!r}")
        path = (self.root / user / slug).resolve()
        if not str(path).startswith(str(self.root.resolve()) + os.sep):
            raise BrokerError("path escapes the workspace")
        return path


async def _git(args: list[str], cwd: Path | None = None,
               env_extra: dict[str, str] | None = None) -> GitResult:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env_extra or {})}
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise BrokerError(f"git {args[0]} timed out after {_GIT_TIMEOUT_S}s", status=504)
    return GitResult(proc.returncode == 0, out.decode(errors="replace"),
                     err.decode(errors="replace"))


class Credential:
    """A project's credential, alive only inside a `with` block.

    An SSH key has to be a file for git to use it, so it becomes one — 0600, in
    a private temp directory outside the checkout, removed on the way out even
    if the operation raised. A token never touches the disk at all, and neither
    does a `basic` account and password: those are for `deploy()`, which hands
    them to one subprocess through its environment.
    """

    def __init__(self, kind: str, secret: str, user: str = "") -> None:
        self.kind = kind
        self.secret = secret or ""
        # The account half of a `basic` credential. Not used by git -- that
        # kind is for the deploy script, which needs both halves and gets them
        # from the environment rather than from the model.
        self.user = user or ""
        self._dir: str | None = None

    def __enter__(self) -> "Credential":
        if self.kind == "ssh_key" and self.secret:
            self._dir = tempfile.mkdtemp(prefix="broker-key-")
            os.chmod(self._dir, stat.S_IRWXU)
            key = Path(self._dir) / "id"
            key.write_text(self.secret.rstrip("\n") + "\n", encoding="utf-8")
            key.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def git_args(self) -> list[str]:
        """Arguments that authenticate this one invocation and persist nothing."""
        if self.kind == "token" and self.secret:
            # -c rather than a URL with the token in it: `git remote -v` and
            # `.git/config` stay clean, which is where this usually leaks.
            return ["-c", f"http.extraHeader=Authorization: Bearer {self.secret}"]
        return []

    def env(self) -> dict[str, str]:
        if self._dir:
            return {
                "GIT_SSH_COMMAND":
                    f"ssh -i {Path(self._dir) / 'id'} -o IdentitiesOnly=yes "
                    "-o StrictHostKeyChecking=accept-new -o BatchMode=yes",
            }
        return {}


# A deploy is allowed to take longer than a git operation and still be
# working -- uploads, installs, a remote restart -- but not forever: a hung
# script holds a broker worker and the agent waits on a turn that will never
# come back.
_DEPLOY_TIMEOUT_S = 900


def _hide(text: str, secret: str) -> str:
    """A deploy script's output with the password taken back out of it.

    `_scrub` and then an exact replace. Not paranoia about this stack: `set -x`
    is ordinary in a deploy script and prints every expansion, so a password
    handed over in the environment comes straight back in stderr -- and that
    output goes to the agent, into a conversation, and into a history file,
    which is publishing it rather than logging it.

    The extra replace is because `_scrub` deliberately ignores anything six
    characters or shorter. That is a sensible floor when it is keeping a key
    out of a git error, where a short string would mangle unrelated output; it
    is not a floor to rely on for a password somebody chose.
    """
    out = _scrub(text, secret)
    return out.replace(secret, "***") if secret else out


async def deploy(workspace: Workspace, user: str, slug: str, project: dict,
                 credential: "Credential") -> dict:
    """Run the project's own deploy script, with its credential in the
    environment and out of everybody else's reach.

    This is the point of the whole arrangement. The agent can ask for a deploy
    and read what happened; it never holds the password, because the password
    is handed to a subprocess it does not own. Letting the agent run the script
    itself through `bash` would mean giving it the secret first, and a secret
    an agent holds is a secret in the next conversation.

    The script is named by the project, not by the caller. `deploy_path` is a
    setting an admin wrote on the projects page, and resolving it inside the
    checkout is what keeps "run the deploy" from becoming "run anything".
    """
    workspace.require_marker()
    root = workspace.path_for(user, slug)
    if not (root / ".git").is_dir():
        raise BrokerError(f"{slug} is not checked out yet", status=409)

    # The script itself, when the project holds one. Written to a private
    # directory outside the checkout rather than into the repository: it is not
    # part of the project's source, it would show up in every diff and status
    # if it were, and a file the deploy writes into the tree it is deploying is
    # a file somebody eventually commits by accident.
    body = (project.get("deploy_script") or "").strip()
    rel = (project.get("deploy_path") or "").strip()
    tmp_dir = None
    if body:
        tmp_dir = tempfile.mkdtemp(prefix="broker-deploy-")
        os.chmod(tmp_dir, stat.S_IRWXU)
        script = Path(tmp_dir) / "deploy.sh"
        script.write_text(body.rstrip("\n") + "\n", encoding="utf-8")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR)
        rel = "(the project's deploy script)"
        # `-e`, so a line that fails stops the deploy. Without it `sh` carries
        # on and exits with the status of the *last* command, so an upload that
        # 401'd followed by an `echo done` reports `ok: true` -- a deploy that
        # went green while doing nothing, which is the failure this stack is
        # least able to see. Only on this branch: a script stored in a
        # repository is somebody's existing file and may lean on the old
        # behaviour, and this field is new enough that nothing does yet.
        argv = ["/bin/sh", "-e", str(script)]
    elif rel:
        # The older shape: a path to a file inside the repository. Resolved and
        # then checked to still be inside the checkout -- "../../" in a stored
        # setting would otherwise run something outside the repo with a live
        # credential in its environment.
        script = (root / rel).resolve()
        if not str(script).startswith(str(root.resolve()) + os.sep):
            raise BrokerError("the deploy script is outside the checkout",
                              status=400)
        if not script.is_file():
            raise BrokerError(f"no deploy script at {rel}", status=404)
        argv = ["/bin/sh", str(script)]
    else:
        raise BrokerError(
            f"{slug} has no deploy script -- write one on the projects page "
            f"before asking for a deploy.", status=400)

    env = {
        **os.environ,
        "DEPLOY_PROJECT": slug,
        "DEPLOY_KIND": credential.kind or "none",
        "DEPLOY_USER": credential.user or "",
        "DEPLOY_PASSWORD": credential.secret or "",
    }
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(root),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(),
                                          timeout=_DEPLOY_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise BrokerError(
            f"the deploy script timed out after {_DEPLOY_TIMEOUT_S}s", status=504)
    finally:
        # Even on the timeout, and even if the script left something behind:
        # the file is 0600 and short-lived, but it is a file the deploy wrote
        # and nothing else will come back for it.
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    secret = credential.secret or ""
    # What was actually deployed. The script runs against the checkout as it
    # stands, so the branch it happened to be on decides what went out -- and
    # since a merge leaves the checkout on the trunk, the ordinary sequence
    # deploys the trunk. Reported rather than assumed: "it is live" and "it is
    # live from a branch nobody merged" are different sentences, and the person
    # cannot tell them apart unless this says which.
    described = await _describe(root)
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "script": rel,
        "branch": described["branch"],
        "dirty": described["dirty"],
        # Tail, not everything: a deploy log can be megabytes and all of it
        # would be read into a conversation.
        "stdout": _hide(out.decode(errors="replace"), secret)[-8000:],
        "stderr": _hide(err.decode(errors="replace"), secret)[-4000:],
    }


async def checkout(workspace: Workspace, user: str, slug: str, git_url: str,
                   credential: Credential) -> dict:
    """Clone the project, or fetch it if it is already here. Idempotent.

    Fetch rather than pull: this brings the remote's state into the checkout
    without touching whatever the agent has in its working tree, so a task in
    progress is never rearranged underneath it by somebody else asking for a
    refresh.
    """
    workspace.require_marker()
    path = workspace.path_for(user, slug)
    path.parent.mkdir(parents=True, exist_ok=True)

    with credential as cred:
        args, env = cred.git_args(), cred.env()
        if (path / ".git").is_dir():
            result = await _git([*args, "fetch", "--all", "--prune"], cwd=path, env_extra=env)
            action = "fetched"
        else:
            result = await _git([*args, "clone", "--", git_url, str(path)], env_extra=env)
            action = "cloned"

    if not result.ok:
        # The remote's own words, minus anything that looks like the credential
        # we just used — an auth failure often echoes what it was given.
        detail = _scrub(result.stderr, cred.secret)
        raise BrokerError(f"git {action[:-2]} failed: {detail[:400]}", status=502)

    logger.info("code-broker: {} {} for {}", action, slug, user)
    return {"action": action, "path": str(path), **await _describe(path)}


async def status(workspace: Workspace, user: str, slug: str) -> dict:
    workspace.require_marker()
    path = workspace.path_for(user, slug)
    if not (path / ".git").is_dir():
        raise BrokerError(f"{slug} is not checked out yet", status=404)
    return {"path": str(path), **await _describe(path)}


# The branches nobody works on directly. Not a list of names the house happens
# to use — it is the shape of the rule in ALFRED_PROGRAMADOR.md: the agent
# proposes a branch and a person merges it, so the branch it commits to can
# never be the one that would land the change on its own.
_TRUNK = {"main", "master", "trunk", "develop", "HEAD"}

# A remote refusing the push because of a branch policy, as opposed to refusing
# it because something is wrong. Azure DevOps, GitHub and GitLab each phrase it
# differently and all three mean the same thing: this branch is merged through a
# pull request, by a person, and no retry here will change that. Worth telling
# apart from a real failure, because the answer is "open the request", not "try
# again" -- and because a household reading "git push failed" about a policy
# working exactly as configured learns nothing.
_POLICY_REFUSAL = re.compile(
    r"(protected branch|branch policy|policies|pull request|merge request|"
    r"TF402455|GH006|pre-receive hook declined)", re.I)


def _checked_out(workspace: Workspace, user: str, slug: str) -> Path:
    workspace.require_marker()
    path = workspace.path_for(user, slug)
    if not (path / ".git").is_dir():
        raise BrokerError(f"{slug} is not checked out yet", status=404)
    return path


def _refuses(paths: list[str], protected: list[str]) -> list[str]:
    """Which of `paths` the project refuses, and why this over-matches.

    A pattern is tried against the whole path, the file name, and every
    directory along the way, so `*.pem` catches `deploy/certs/api.pem` and
    `.env` catches `services/api/.env`. That is wider than a literal reading —
    deliberately. This is the list that decides whether a credential can be
    committed, and the cost of refusing one file too many is a sentence
    explaining why, while the cost of missing one is a key in the history of a
    repo somebody else can read.
    """
    hits = []
    for path in paths:
        parts = PurePosixPath(path).parts
        candidates = {path, *parts}
        if any(fnmatch(c, pattern) for pattern in protected for c in candidates):
            hits.append(path)
    return hits


async def branch(workspace: Workspace, user: str, slug: str, name: str) -> dict:
    """Start a branch and move onto it. Local: no remote, no credential."""
    path = _checked_out(workspace, user, slug)
    name = (name or "").strip()
    if not _SAFE_BRANCH.match(name):
        raise BrokerError(
            f"«{name}» is not a branch name I will make: letters, digits, "
            "'-', '_', '.' and '/', starting with a letter or digit", status=400)
    if name.split("/")[-1] in _TRUNK or name in _TRUNK:
        raise BrokerError(
            f"«{name}» is a trunk name. Work goes on a branch of its own and "
            "somebody in the house merges it — see ALFRED_PROGRAMADOR.md",
            status=403)
    if not name.startswith(_BRANCH_PREFIX):
        name = _BRANCH_PREFIX + name
    existing = await _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=path)
    # Switching to a branch that is already here is what somebody continuing
    # yesterday's work means; making it twice is an error they did not make.
    result = await _git(["switch", name] if existing.ok else ["switch", "-c", name], cwd=path)
    if not result.ok:
        raise BrokerError(f"git switch failed: {result.stderr.strip()[:400]}", status=502)
    logger.info("code-broker: {} switched {} to {}", user, slug, name)
    return {"action": "switched" if existing.ok else "created",
            "path": str(path), **await _describe(path)}


async def commit(workspace: Workspace, user: str, slug: str, message: str,
                 protected: list[str] | None = None, asked_by: str = "",
                 author: str = "") -> dict:
    """Stage what changed and commit it. Local: nothing reaches a remote.

    Two refusals, and neither is about git. A commit on a trunk branch is the
    change landing without anybody looking at it, and a commit that touches a
    protected path is the floor in the registry meaning something for the first
    time — until now it was a list the broker was handed and never read.
    """
    path = _checked_out(workspace, user, slug)
    message = (message or "").strip()
    if not message:
        raise BrokerError("a commit needs a message saying what it does", status=400)

    described = await _describe(path)
    if described["branch"] in _TRUNK:
        raise BrokerError(
            f"this checkout is on «{described['branch']}». Make a branch first: "
            "the house merges, you propose", status=403)
    if not described["dirty"]:
        raise BrokerError("nothing changed, so there is nothing to commit", status=400)

    refused = _refuses(described["changed"], protected or [])
    if refused:
        raise BrokerError(
            "this project refuses these paths, so the commit was not made: "
            + ", ".join(refused[:10]), status=403)

    staged = await _git(["add", "--all"], cwd=path)
    if not staged.ok:
        raise BrokerError(f"git add failed: {staged.stderr.strip()[:400]}", status=502)
    # Staging can widen the set — a rename shows as one path unstaged and two
    # staged — so the floor is checked again against what will actually go in.
    names = await _git(["diff", "--cached", "--name-only"], cwd=path)
    refused = _refuses([n for n in names.stdout.splitlines() if n], protected or [])
    if refused:
        await _git(["reset"], cwd=path)
        raise BrokerError(
            "this project refuses these paths, so the commit was not made: "
            + ", ".join(refused[:10]), status=403)

    # The person owns it, the agent is named beside them. Falling back to the
    # agent as author when the registry sent no name keeps the commit honest:
    # better an obviously-automated author than somebody's name on a commit
    # this process could not attribute.
    name = author or _AGENT_NAME
    email = f"{asked_by}@example.com" if author and asked_by else _AGENT_EMAIL
    agent = f"{_AGENT_NAME.replace('nanobot', user)}" if user else _AGENT_NAME
    message = f"{message}\n\nCo-authored-by: {agent} <{_AGENT_EMAIL}>"
    result = await _git(["-c", f"user.name={name}",
                         "-c", f"user.email={email}",
                         "commit", "-m", message], cwd=path)
    if not result.ok:
        raise BrokerError(f"git commit failed: {result.stderr.strip()[:400]}", status=502)
    logger.info("code-broker: {} committed to {} on {}", user, slug, described["branch"])
    return {"action": "committed", "path": str(path), **await _describe(path)}


async def push(workspace: Workspace, user: str, slug: str,
               credential: Credential) -> dict:
    """Publish the current branch so a person can look at it and merge it.

    The one verb here that talks to a remote, and the one that has to refuse a
    trunk branch absolutely: pushing to `main` *is* merging, whatever it is
    called on the way in.
    """
    path = _checked_out(workspace, user, slug)
    described = await _describe(path)
    head = described["branch"]
    if head in _TRUNK:
        raise BrokerError(
            f"pushing «{head}» is merging, and that is a person's decision. "
            "Put the work on a branch and push that", status=403)
    if described["dirty"]:
        raise BrokerError(
            "there are uncommitted changes; commit them first so what you "
            "push is what you tested", status=400)

    with credential as cred:
        result = await _git([*cred.git_args(), "push", "--set-upstream", "origin", head],
                            cwd=path, env_extra=cred.env())
        if not result.ok:
            detail = _scrub(result.stderr, cred.secret)
            raise BrokerError(f"git push failed: {detail[:400]}", status=502)
    logger.info("code-broker: {} pushed {} of {}", user, head, slug)
    return {"action": "pushed", "branch": head, "path": str(path), **described}


async def _trunk_of(path: Path) -> str:
    """The branch this project calls its trunk, asked rather than assumed.

    `origin/HEAD` is what the remote itself says, and it is right whenever the
    clone recorded it. Where it is missing -- an older clone, a remote that
    never published a default -- the fallback is whichever of the usual names
    the remote actually has. "Neither" is worth stopping on rather than
    guessing `main` at a repository whose trunk is `master`.
    """
    head = await _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=path)
    if head.ok and head.stdout.strip():
        return head.stdout.strip().rsplit("/", 1)[-1]
    for name in ("main", "master"):
        found = await _git(["rev-parse", "--verify", f"refs/remotes/origin/{name}"], cwd=path)
        if found.ok:
            return name
    raise BrokerError(
        "this repository has no origin/main or origin/master and origin/HEAD "
        "says nothing, so there is no trunk to merge into. Somebody has to say "
        "which branch that is", status=409)


async def merge(workspace: Workspace, user: str, slug: str,
                credential: Credential) -> dict:
    """Put the branch on the trunk and publish it: the last step of a finished job.

    `push` refuses a trunk branch on purpose -- pushing to main *is* merging,
    and that must never happen because a branch name was wrong. This is the
    same act done deliberately, with the checks that belong to it.

    It exists because the other half of that rule was not true. "The merge is
    done by someone with permissions over master" left every approved task
    sitting on a branch nobody merged: the person who approves the work is the
    person whose decision it is, and they have just made it.

    What this will not do is decide anything. A conflict, a diverged trunk, or
    a remote that refuses the push all stop here and say so -- those are the
    cases that need a person's judgement rather than their keystroke.
    """
    path = _checked_out(workspace, user, slug)
    described = await _describe(path)
    head = described["branch"]
    if head in _TRUNK:
        raise BrokerError(
            f"«{head}» is already the trunk, so there is nothing to merge. "
            "Work goes out on a branch and the branch comes back here", status=400)
    if described["dirty"]:
        raise BrokerError(
            "there are uncommitted changes; commit them first so what is "
            "merged is what was reviewed", status=400)

    with credential as cred:
        args, env = cred.git_args(), cred.env()
        fetched = await _git([*args, "fetch", "--prune", "origin"], cwd=path, env_extra=env)
        if not fetched.ok:
            raise BrokerError(
                f"git fetch failed: {_scrub(fetched.stderr, cred.secret)[:400]}",
                status=502)
        trunk = await _trunk_of(path)

        switched = await _git(["checkout", trunk], cwd=path)
        if not switched.ok:
            raise BrokerError(
                f"could not switch to «{trunk}»: {switched.stderr.strip()[:200]}",
                status=409)
        # Only if the trunk can be brought up to date cleanly. A local trunk
        # that has drifted from the remote is somebody else's unpushed work,
        # and flattening it here would be this agent losing a commit it never
        # saw.
        forwarded = await _git(["merge", "--ff-only", f"origin/{trunk}"], cwd=path)
        if not forwarded.ok:
            await _git(["checkout", head], cwd=path)
            raise BrokerError(
                f"the local «{trunk}» has diverged from the remote and cannot be "
                "brought up to date before merging. Somebody has to decide how "
                "those two histories meet", status=409)

        # Where the trunk stood before, so the answer can tell "I merged it"
        # from "there was nothing to merge". `git merge` exits 0 for both, and
        # reporting the second as the first is how this said «merge listo» about
        # work somebody else had already put there -- true-sounding, and no way
        # for the person to know the difference.
        was = await _git(["rev-parse", "HEAD"], cwd=path)
        before = was.stdout.strip() if was.ok else ""

        # `--no-ff`, so the trunk's history says that a change arrived as one
        # piece and which branch it came from. A fast-forward scatters the
        # commits along the trunk and loses exactly what somebody reading the
        # log a year from now is looking for.
        # With an identity, because `--no-ff` writes a commit and this checkout
        # deliberately has none configured -- the same reason `commit` passes
        # its own. Without it git refuses with "Committer identity unknown",
        # which this then reported as a merge conflict: a clean fast-forward
        # came back as «hay conflicto», about two histories that had none.
        merged = await _git(
            ["-c", f"user.name={_AGENT_NAME}", "-c", f"user.email={_AGENT_EMAIL}",
             "merge", "--no-ff", "-m", f"Merge branch '{head}'", head], cwd=path)
        if not merged.ok:
            # Abort, then back where we started: a half-merged trunk in a
            # checkout nobody is watching is the worst place to leave this.
            await _git(["merge", "--abort"], cwd=path)
            await _git(["checkout", head], cwd=path)
            # A conflict and a merge that could not run are different problems
            # with different answers, and calling everything a conflict sends
            # the person to look for one that is not there. Git says which.
            detail = (merged.stdout + "\n" + merged.stderr).strip()
            if "CONFLICT" in detail or "Automatic merge failed" in detail:
                raise BrokerError(
                    f"«{head}» conflicts with «{trunk}», and a conflict is a "
                    "person's to resolve. Nothing was merged and the branch is "
                    f"untouched: {detail[:200]}", status=409)
            raise BrokerError(
                f"the merge of «{head}» into «{trunk}» could not be made, and "
                f"it is not a conflict: {detail[:300]}", status=502)

        pushed = await _git([*args, "push", "origin", trunk], cwd=path, env_extra=env)
        if not pushed.ok:
            detail = _scrub(pushed.stderr, cred.secret)
            # Merged locally, refused by the remote. Undoing it keeps the
            # checkout honest -- left alone, the next call reports "already up
            # to date" about a trunk only this machine has.
            await _git(["reset", "--hard", f"origin/{trunk}"], cwd=path)
            await _git(["checkout", head], cwd=path)
            if _POLICY_REFUSAL.search(detail):
                raise BrokerError(
                    f"«{trunk}» does not take pushes directly -- the remote asks "
                    "for a pull request. The branch is pushed and ready, and "
                    "opening that request is the part that needs a person: "
                    f"{detail[:300]}", status=409)
            raise BrokerError(f"git push failed: {detail[:400]}", status=502)

    now = await _git(["rev-parse", "HEAD"], cwd=path)
    after = now.stdout.strip() if now.ok else ""
    # Not knowing is not "already there". If either `rev-parse` did not answer,
    # this says nothing rather than turning silence into the positive claim
    # that the work was already on the trunk -- which is the exact sentence
    # this whole branch exists to stop being said falsely.
    moved = before != after if (before and after) else True
    action = "merged" if moved else "already-merged"
    logger.info("code-broker: {} {} {} into {} of {}",
                user, action, head, trunk, slug)
    # The description first, so the names below win. `_describe` runs after the
    # merge and reports the *current* branch, which is now the trunk -- spread
    # last it overwrote `branch` with "main", and the answer to "what did you
    # merge?" became the name of the thing it was merged into.
    return {**await _describe(path), "action": action, "branch": head,
            "trunk": trunk, "on": trunk, "path": str(path),
            # Said plainly, because "already there" is a different fact about
            # the world from "I put it there" and the person is entitled to it.
            "note": ("" if moved else
                     f"«{head}» was already contained in «{trunk}» -- nothing "
                     f"to merge. The trunk was pushed as it stands."),
            }


async def pull(workspace: Workspace, user: str, slug: str,
               credential: Credential) -> dict:
    """Bring the current branch up to date, and only if that is safe.

    Fast-forward only. A merge here would be the agent resolving somebody
    else's conflict in a checkout nobody is watching; a rebase would rewrite
    commits it did not make. Both are answers a person should give, so this
    stops and says the branch has diverged instead.
    """
    path = _checked_out(workspace, user, slug)
    described = await _describe(path)
    if described["dirty"]:
        raise BrokerError(
            "there are uncommitted changes; a pull would rearrange them. "
            "Commit or set them aside first", status=400)

    with credential as cred:
        args, env = cred.git_args(), cred.env()
        fetched = await _git([*args, "fetch", "--prune", "origin"], cwd=path, env_extra=env)
        if not fetched.ok:
            raise BrokerError(
                f"git fetch failed: {_scrub(fetched.stderr, cred.secret)[:400]}", status=502)
        result = await _git(["merge", "--ff-only", "@{upstream}"], cwd=path)

    if not result.ok:
        err = result.stderr.strip()
        if "no upstream" in err.lower() or "@{upstream}" in err:
            raise BrokerError(
                f"«{described['branch']}» is not tracking a remote branch yet — "
                "push it first, or there is nothing to pull", status=400)
        raise BrokerError(
            f"«{described['branch']}» has diverged from the remote, so it cannot "
            "be fast-forwarded. Somebody has to decide how those two histories "
            f"meet: {err[:200]}", status=409)
    logger.info("code-broker: {} pulled {} of {}", user, described["branch"], slug)
    return {"action": "pulled", "path": str(path), **await _describe(path)}


async def _describe(path: Path) -> dict:
    """Branch, head and whether anything is uncommitted."""
    branch = await _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    head = await _git(["rev-parse", "--short", "HEAD"], cwd=path)
    dirty = await _git(["status", "--porcelain"], cwd=path)
    changed = [line[3:] for line in dirty.stdout.splitlines() if line.strip()]
    return {
        "branch": branch.stdout.strip(),
        "head": head.stdout.strip(),
        "dirty": bool(changed),
        "changed": changed[:50],
    }


def _scrub(text: str, secret: str) -> str:
    """Keep a secret out of an error on its way back to the agent."""
    if secret and len(secret) > 6:
        text = text.replace(secret, "***")
        for line in secret.splitlines():
            if len(line) > 12:
                text = text.replace(line, "***")
    return text
