"""The household's own capabilities, offered to opencode as MCP tools.

opencode brings its own `read`, `grep`, `edit` and `bash`; this brings the four
things it has no way to know about -- the code broker, the file share, the
Designer, and enough of the house to answer "where am I".

Two rules run through all of it:

* **Identity is the header, never an argument.** No tool takes a user. See
  identity.py; the short version is that a prompt is something anybody who can
  get text in front of the model can write.
* **Errors come back as sentences.** The thing reading these is a model
  deciding what to do next, and a traceback tells it nothing it can act on. A
  refused push should read as a refused push, not as a 502.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from alfred_mcp import config
from alfred_mcp.broker import Broker, BrokerDown
from alfred_mcp.code_tasks import CodeTasks, CodeTaskError
from alfred_mcp.delegate import Delegate, DelegateError
from alfred_mcp.identity import Denied, authenticate
from alfred_mcp.share import Share, ShareError

SETTINGS = config.load()

_broker = Broker(SETTINGS.broker_url, SETTINGS.member, SETTINGS.broker_token,
                 SETTINGS.workspace_broker, SETTINGS.workspace_agent)
_share = Share(SETTINGS.homecore_url, SETTINGS.login, SETTINGS.homecore_token,
               SETTINGS.folder, SETTINGS.verify_tls)
_delegate = Delegate(SETTINGS.homecore_url, SETTINGS.login,
                     SETTINGS.homecore_token, SETTINGS.verify_tls)
_code = CodeTasks(SETTINGS.homecore_url, SETTINGS.login,
                  SETTINGS.homecore_token, SETTINGS.verify_tls)

# Where the broker checks projects out. Mounted read-only here so a file the
# agent produced *in a checkout* can be handed to the person without being
# pasted through the model. Read-only on purpose: writing to a checkout is the
# agent's own `edit`, and going through here would put a second, unwatched
# writer on the same tree.
WORKSPACE = Path(os.environ.get("CODE_WORKSPACE_DIR", "/nanobot-code-workspace"))

mcp = FastMCP("alfred", host=SETTINGS.bind, port=SETTINGS.port,
              streamable_http_path="/mcp")


def tool(fn):
    """Authenticate, then turn any expected failure into a readable sentence.

    Wrapping rather than repeating: an unauthenticated path through any one of
    these is the whole security model gone, and the way that happens is
    somebody adding a tool and forgetting the first line.
    """
    @functools.wraps(fn)
    def wrapper(ctx: Context, *args, **kwargs):
        try:
            authenticate(ctx, SETTINGS.member, SETTINGS.token)
        except Denied as exc:
            return f"Refused: {exc}"
        try:
            return fn(ctx, *args, **kwargs)
        except (BrokerDown, ShareError, DelegateError,
                CodeTaskError) as exc:
            return f"Could not do that: {exc}"
        except Exception as exc:                                  # noqa: BLE001
            # Never the traceback: it would carry paths and, on a bad day, a
            # token. The type is enough for whoever reads the container log.
            return (f"Could not do that: an unexpected {type(exc).__name__}. "
                    f"The details are in the alfred-mcp container's log.")
    return wrapper


# --- the code broker --------------------------------------------------------

@mcp.tool()
@tool
def list_projects(ctx: Context) -> dict:
    """The projects this household member may work on.

    Start here. A project not on this list cannot be opened, and asking for one
    by name gets the same answer as asking for one that does not exist.
    """
    return _broker.list_projects()


@mcp.tool()
@tool
def checkout_project(ctx: Context, project: str) -> dict:
    """Clone or refresh a project's checkout and say where it is.

    Do this before reading or editing anything in it. The path it returns is
    the one to point `read`, `grep` and `edit` at.
    """
    return _broker.checkout(project)


@mcp.tool()
@tool
def project_status(ctx: Context, project: str) -> dict:
    """The checkout's branch and what is modified, staged or untracked."""
    return _broker.status(project)


@mcp.tool()
@tool
def create_branch(ctx: Context, project: str, name: str) -> dict:
    """Start a branch, before the first edit.

    Not before the commit -- before the *edit*. Committing refuses on main and
    master, so editing first leaves the work stranded on trunk and it has to be
    moved before it can be saved.
    """
    return _broker.branch(project, name)


@mcp.tool()
@tool
def commit_changes(ctx: Context, project: str, message: str) -> dict:
    """Commit what is in the checkout, with a message.

    Use this rather than `git commit` in a shell. The broker is what checks the
    project's protected paths and puts the right names on the commit; a commit
    made another way skips both and nothing about it fails visibly.
    """
    return _broker.commit(project, message)


@mcp.tool()
@tool
def push_branch(ctx: Context, project: str) -> dict:
    """Push the current branch to the project's remote.

    Not the end of the job. Once the person has looked at the work and said it
    is right, `merge_branch` puts it on the trunk -- a change that only exists
    on a branch is a change nobody got.
    """
    return _broker.push(project)


@mcp.tool()
@tool
def merge_branch(ctx: Context, project: str) -> dict:
    """Merge the pushed branch into the project's trunk and publish it.

    The last step of an approved job, and only ever after the person has said
    the work is right. Finishing means the change is on main, not that it is on
    a branch waiting for somebody who is not coming.

    It stops and says so, changing nothing, in the three cases that are a
    person's to decide: the branch conflicts with the trunk, the local trunk has
    diverged from the remote, or the remote requires a pull request. Repeat that
    answer to the person rather than working around it.

    Read `action` before saying it is done. `merged` means the trunk moved
    because of this call; `already-merged` means the branch was already
    contained in the trunk and nothing was merged — somebody else had put it
    there. Say which one happened, in the words of `note`. "Already there" is a
    different fact about the world from "I put it there", and reporting the
    second when it was the first is the thing this call was changed to stop.
    """
    return _broker.merge_branch(project)


@mcp.tool()
@tool
def pull_project(ctx: Context, project: str) -> dict:
    """Bring the checkout up to date with its remote."""
    return _broker.pull(project)


# --- handing something over -------------------------------------------------

@mcp.tool()
@tool
def write_deploy_script(ctx: Context, project: str, script: str) -> dict:
    """Write the script that deploys a project. Replaces whatever was there.

    A real script, stored with the project -- not a path to a file inside its
    source. It runs with the checkout as its working directory.

    **Never put a password in it.** The credential attached to the project
    arrives as `$DEPLOY_USER` and `$DEPLOY_PASSWORD` when the script runs, and
    its value is removed from the output afterwards. A secret written into the
    text instead is in every copy, diff and backup of that script forever.

    It runs under `sh -e`, so the first line that fails stops it.

    Show the person what you are about to store and what it will do, in plain
    words, before you write it. If `list_projects` says the project already has
    one (`has_deploy_script`), **read it with `read_deploy_script` first** --
    replacing somebody's working deploy with a guess is the kind of mistake
    that is only discovered at the worst moment. `project_status` is the
    checkout's branch and modified files and says nothing about this.
    """
    return _code.set_deploy_script(project, script)


@mcp.tool()
@tool
def read_deploy_script(ctx: Context, project: str) -> dict:
    """The deploy script as it is stored right now, and which account it uses.

    Read it before saying anything about what a deploy does, and **always after
    one fails**: what you remember writing is not what is stored if anybody has
    edited it since, and repeating a diagnosis from memory is how "the host does
    not resolve" outlived the host being fixed.

    It answers with `deploy_script`, the `deploy_user` the script will be handed,
    and `deploy_auth_kind`. Never the password -- that reaches the script as
    `$DEPLOY_PASSWORD` at run time and is not yours to see, which is also why a
    login failure is something to report rather than to debug by guessing.
    """
    return _broker.deploy_script(project)


@mcp.tool()
@tool
def deploy_project(ctx: Context, project: str) -> dict:
    """Run a project's own deploy script, and report what it printed.

    The script is the one configured for the project on the projects page --
    you do not choose it and you cannot pass arguments. Its credential is put
    into the script's environment by the broker (`DEPLOY_USER`,
    `DEPLOY_PASSWORD`, `DEPLOY_KIND`); you never see it, and the password is
    taken back out of the output before you are shown it.

    Use this rather than running the script yourself through the shell. Doing
    it that way would need the password handed to you first, and a password in
    a conversation is a password in every summary and history file after it.

    Check the project out first. Read `exit_code`: a script can fail and still
    print reassuring things.
    """
    return _broker.deploy(project)


@mcp.tool()
@tool
def save_text(ctx: Context, name: str, text: str, subdir: str = "") -> str:
    """Save text into the person's own folder and return the link for the chat.

    For a report, a diff, a log, a .csv -- anything they will want to open,
    keep or read later. Answer with the link exactly as it comes back; a
    `download:` link written from memory does not fail when you write it, it
    fails when they click it, and from here that looks like it worked.

    If it is short enough to read in the chat, paste it there instead and save
    nothing.
    """
    return _share.save_bytes(name, text.encode("utf-8"), subdir)


@mcp.tool()
@tool
def share_project_file(ctx: Context, project: str, path: str,
                       subdir: str = "") -> str:
    """Copy a file out of a checkout onto the share and return its link.

    `path` is relative to the project's checkout. Use this for something that
    already exists as a file -- a generated report, a test log, an exported
    .csv -- rather than reading it and passing the contents back through
    `save_text`.
    """
    root = (WORKSPACE / SETTINGS.member / project).resolve()
    target = (root / path).resolve()
    # Anchored, not merely joined. `..` in `path` is the obvious way this
    # becomes "read any file this container can see", and the agent composing
    # that path is reading text somebody else may have written.
    if not target.is_relative_to(root):
        return "Refused: that path is outside the project's checkout."
    if not target.is_file():
        return (f"Could not do that: {path} is not a file in {project}'s "
                f"checkout. `project_status` shows what is there.")
    return _share.save_bytes(target.name, target.read_bytes(), subdir)


# --- the other Alfreds ------------------------------------------------------

@mcp.tool()
@tool
def delegate_to_designer(ctx: Context, brief: str) -> dict:
    """Commission a visual piece -- a diagram, a chart, a sheet, a page.

    You do not make these, even when you know the HTML: the Designer has
    different rules and a different model, and a piece of yours beside one of
    theirs shows.

    **The brief is all they see.** Not this conversation, not who asked, not
    what for. A complete one carries: which piece and what format it lives in;
    the exact content, already written -- the boxes and the arrows, the data,
    the text, looked up first if it has to be; who will read it and what they
    have to understand; and anything not negotiable, like size or language.

    When it comes back, hand over what they gave you as your own and copy their
    links verbatim. There is no back and forth -- it is a commission, not a
    conversation.
    """
    return _delegate.to("designer", brief)


# --- where am I -------------------------------------------------------------

@mcp.tool()
@tool
def ask_code(ctx: Context, task: str, project: str = "") -> dict:
    """Hand a coding job to opencode, which does the work in a real checkout.

    Use this for anything that means *changing* a project: writing code, fixing
    a failing test, a refactor, a dependency bump. Not for questions about code
    you can already read -- answer those yourself; they are faster, and this
    costs a whole agent run.

    It returns as soon as the job is registered, NOT when the work is done. Say
    so plainly: give the task id, say you will report back, and carry on with
    the conversation. Do not poll in a loop -- the result is announced in this
    chat by itself when it lands, and `code_task_status` is for when somebody
    asks in the meantime.

    Write the task the way you would brief a competent colleague who cannot see
    this conversation: which project, what outcome, and how to know it worked.
    It does not get the chat history, and a thin brief comes back as thin work.

    Say in the task that it must branch and commit through the broker tools
    rather than its own shell. That is what keeps the access list, the protected
    paths and the co-author line -- none of which fail loudly when bypassed.
    """
    brief = task if not project else f"In the project `{project}`:\n\n{task}"
    got = _code.start(brief, label=task[:160])
    return {"task_id": got.get("task_id", ""),
            "status": "started",
            "note": ("Registered and running. The answer arrives in this chat "
                     "when it finishes; there is nothing to wait for here.")}


@mcp.tool()
@tool
def code_task_status(ctx: Context, task_id: str) -> dict:
    """Where a handed-off coding job got to, for when somebody asks.

    `running` means exactly that -- report it and leave it alone. The finished
    answer is announced in the chat on its own, so there is no reason to call
    this on a timer.
    """
    return _code.status(task_id)


@mcp.tool()
@tool
def house_context(ctx: Context) -> dict:
    """Which household member this session belongs to, and where things are.

    Useful once at the start of a session about the house itself. It names no
    credential and nothing that is not already visible to whoever is typing.
    """
    return {
        "member": SETTINGS.member,
        "share_folder": SETTINGS.folder,
        # The agent's own view of it, not this container's -- `house_context`
        # is read by opencode, which runs on the host. See broker.retarget.
        "checkout_root": str(Path(SETTINGS.workspace_agent
                                  or SETTINGS.workspace_broker)
                             / SETTINGS.member),
        "note": ("Files handed to the person go on the share via save_text or "
                 "share_project_file. A path inside a container is one nobody "
                 "else can open."),
    }


# --- is this thing working ---------------------------------------------------

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Whether this container can do its job, not merely whether it is running.

    The MCP endpoint itself is a poor health check -- it needs a handshake and
    a session, so anything simple enough for a compose `healthcheck:` would end
    up asserting that the port is open, which is the check this stack keeps
    learning not to write.

    So it reports the thing that actually breaks: the broker being unreachable.
    That is the failure that turns every git verb into a refusal while the
    container looks perfectly healthy -- the same shape as the broker's own
    `"ok": false`, which docker called healthy because the endpoint returned
    200 saying so. The status code carries it here, so a payload-blind checker
    cannot get it wrong.
    """
    from starlette.responses import JSONResponse

    try:
        broker = _broker.health()
        reachable, detail = bool(broker.get("ok")), broker
    except BrokerDown as exc:
        reachable, detail = False, {"error": str(exc)}
    return JSONResponse(
        {"ok": reachable, "member": SETTINGS.member, "broker": detail},
        status_code=200 if reachable else 503)


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
