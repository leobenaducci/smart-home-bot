"""The tool surface: what it exposes, what it refuses, and how it fails."""

import asyncio

import pytest

from alfred_mcp import server
from alfred_mcp.code_tasks import CodeTaskError
from alfred_mcp.share import ShareError, _safe_name, download_link


# --- every tool is behind the gate ------------------------------------------

def test_every_tool_refuses_an_unauthenticated_call():
    """Not a sample of them. An unauthenticated path through any one tool is
    the whole model gone, and the way that happens is somebody adding a tool
    and forgetting the decorator."""
    class _Ctx:
        request_context = type("RC", (), {"request": None})()

    specs = asyncio.run(server.mcp.list_tools())
    assert specs, "no tools registered at all"

    for spec in specs:
        fn = getattr(server, spec.name)
        args = list((spec.inputSchema or {}).get("properties") or {})
        result = fn(_Ctx(), *["x"] * len(args))
        assert isinstance(result, str) and result.startswith("Refused:"), (
            f"{spec.name} answered an unauthenticated call with {result!r}")


# --- share_project_file stays inside the checkout ---------------------------

@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(server, "authenticate", lambda *a, **k: "user1")

    class _Ctx:
        request_context = type("RC", (), {"request": None})()
    return _Ctx()


@pytest.mark.parametrize("escape", [
    "../../../etc/passwd",
    "../user2/secret/notes.md",
    "subdir/../../../../etc/hostname",
])
def test_a_path_escaping_the_checkout_is_refused(authed, monkeypatch, tmp_path,
                                                 escape):
    """`..` is the obvious way this becomes "read any file this container can
    see", and the agent composing the path is reading text somebody else may
    have written."""
    monkeypatch.setattr(server, "WORKSPACE", tmp_path)
    (tmp_path / "user1" / "proj").mkdir(parents=True)
    out = server.share_project_file(authed, "proj", escape)
    assert "outside the project's checkout" in out, out


def test_a_file_that_is_not_there_says_so(authed, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKSPACE", tmp_path)
    (tmp_path / "user1" / "proj").mkdir(parents=True)
    out = server.share_project_file(authed, "proj", "nope.txt")
    assert "not a file" in out, out


def test_a_real_file_is_read_and_handed_to_the_share(authed, monkeypatch,
                                                     tmp_path):
    monkeypatch.setattr(server, "WORKSPACE", tmp_path)
    proj = tmp_path / "user1" / "proj"
    proj.mkdir(parents=True)
    (proj / "report.md").write_text("findings", encoding="utf-8")

    seen = {}

    def _fake(name, data, subdir=""):
        seen.update(name=name, data=data, subdir=subdir)
        return "[report.md](download:testfolder/alfred/report.md)"

    monkeypatch.setattr(server._share, "save_bytes", _fake)
    out = server.share_project_file(authed, "proj", "report.md")
    assert seen == {"name": "report.md", "data": b"findings", "subdir": ""}
    assert out.startswith("[report.md](download:")


# --- failures read as sentences ---------------------------------------------

def test_an_upstream_failure_is_a_sentence_not_a_traceback(authed, monkeypatch):
    def _boom(*a, **k):
        raise ShareError("the share refused that path for this account")
    monkeypatch.setattr(server._share, "save_bytes", _boom)
    out = server.save_text(authed, "x.md", "hello")
    assert out == "Could not do that: the share refused that path for this account"


def test_an_unexpected_failure_does_not_leak_its_detail(authed, monkeypatch):
    """The traceback would carry paths and, on a bad day, a token."""
    def _boom(*a, **k):
        raise RuntimeError("token=hunter2 at /var/lib/home-stack/secrets")
    monkeypatch.setattr(server._share, "save_bytes", _boom)
    out = server.save_text(authed, "x.md", "hello")
    assert "hunter2" not in out and "/var/lib" not in out
    assert "RuntimeError" in out


# --- the download link ------------------------------------------------------

def test_a_plain_name_gets_the_plain_link():
    assert download_link("tomi/alfred/report.md") == \
        "[report.md](download:tomi/alfred/report.md)"


def test_a_name_with_brackets_gets_the_angle_form():
    """Without it the reader's markdown ends the URL at the first ")", so
    "Factura (1).pdf" linked to ".../Factura (1" and died on click."""
    link = download_link("tomi/alfred/Factura (1).pdf")
    assert link == "[Factura (1).pdf](<download:tomi/alfred/Factura (1).pdf>)"


@pytest.mark.parametrize("bad", ["", ".", "..", "   "])
def test_an_unusable_file_name_is_refused(bad):
    with pytest.raises(ShareError):
        _safe_name(bad)


def test_a_name_is_reduced_to_one_segment():
    """Traversal is stripped rather than refused, which is the safer of the two.

    A name is only ever one segment here -- the directory is composed by
    save_bytes from the member's own folder -- so `../../etc/passwd` is not a
    path that gets rejected, it is the file name `passwd` written into the
    person's own alfred/ folder. Refusing instead would be equally safe and
    more brittle: a browser or an editor sends a full path in a filename often
    enough that rejecting them all would break ordinary uploads.
    """
    assert _safe_name("a/b/c.txt") == "c.txt"
    assert _safe_name(r"windows\path\file.md") == "file.md"
    assert _safe_name("../../etc/passwd") == "passwd"


# --- handing work to opencode ------------------------------------------------

def test_code_tools_are_registered():
    """Both halves, because one without the other is unusable: a hand-off with
    no way to ask how it went, or a status call for jobs nothing can start."""
    names = {s.name for s in asyncio.run(server.mcp.list_tools())}
    assert {"ask_code", "code_task_status"} <= names, sorted(names)


def test_a_code_task_failure_reads_as_a_sentence(monkeypatch):
    """CodeTaskError has to be in the wrapper's except list.

    Left out, it falls through to the bare `Exception` arm and the model is told
    "an unexpected CodeTaskError, the details are in the container log" -- which
    is exactly the case where the detail (no coding server for this account) is
    something the assistant could have acted on.
    """
    monkeypatch.setattr(server, "authenticate", lambda *a, **k: None)

    def boom(*a, **k):
        raise CodeTaskError("there is no coding server for this account")

    monkeypatch.setattr(server._code, "start", boom)
    got = server.ask_code(_ctx(), "fix the failing test")
    assert got.startswith("Could not do that:"), got
    assert "no coding server" in got, got
    assert "unexpected" not in got, got


def _ctx():
    return type("Ctx", (), {"request_context":
                            type("RC", (), {"request": None})()})()
