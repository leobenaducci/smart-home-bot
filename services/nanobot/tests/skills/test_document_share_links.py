"""Where a generated document goes, and which link comes back for it.

The link is the whole delivery. It renders whatever it says, and a wrong one
does not fail when Alfred writes it — it fails when the person clicks, with
Alfred none the wiser. That is how "solo se pueden descargar archivos de
media/" kept reaching the family.

So documents are filed in `<their-folder>/alfred/documents/` on the family
share, which is the folder the chat lists as "My files", and the link
points at that copy. The workspace copy is a build artefact and only carries
the link when the share could not be written — a degraded answer, not the
normal one.

The SMB layer is faked in-process; nothing here touches the real share.
"""
import importlib.util
import io
import pathlib
import sys
import types

import pytest

SKILL = (pathlib.Path(__file__).resolve().parents[2]
         / "nanobot" / "skills" / "document" / "create_doc.py")


@pytest.fixture
def doc(tmp_path, monkeypatch):
    """The skill module with a fake share under it. Yields (module, written)."""
    monkeypatch.setenv("FILE_SHARE_FOLDER", "user1")
    monkeypatch.setenv("FILE_SHARE_HOST", "fake.home")
    monkeypatch.setenv("NANOBOT_WORKSPACE", str(tmp_path))

    written = {}

    def rel(unc):
        return unc.replace(r"\\fake.home\share", "").lstrip("\\").replace("\\", "/")

    class Sink(io.BytesIO):
        def __init__(self, unc):
            super().__init__()
            self.unc = unc

        def close(self):
            written[rel(self.unc)] = self.getvalue()
            super().close()

    fake = types.ModuleType("smbclient")
    fake.ClientConfig = lambda **kw: None
    fake.makedirs = lambda unc, exist_ok=False: None
    fake.open_file = lambda unc, mode="rb", **kw: Sink(unc)
    monkeypatch.setitem(sys.modules, "smbclient", fake)

    spec = importlib.util.spec_from_file_location("create_doc_under_test", SKILL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module, written, fake


def _make(module, filename="informe.docx"):
    return module.create({
        "format": "docx", "filename": filename, "title": "Informe",
        "sections": [{"heading": "Uno", "text": "hola"}],
    })


def test_a_document_is_filed_where_the_chat_will_show_it(doc):
    module, written, _ = doc
    result = _make(module)
    assert not result.get("error"), result
    assert result["share_path"] == "user1/alfred/documents/informe.docx"
    # The subdir is a path, not one name: joined whole it produced
    # `user1\alfred/documents\informe.docx` and landed in a folder with a
    # slash in its name.
    assert "user1/alfred/documents/informe.docx" in written, list(written)


def test_the_link_points_at_the_share_copy_not_the_workspace(doc):
    module, _, _ = doc
    result = _make(module)
    assert result["download_link"] == (
        "[📥 Download informe.docx](download:user1/alfred/documents/informe.docx)")
    # The model is told to paste `message` or the link verbatim; they must agree.
    assert result["download_link"] in result["message"]
    assert "download:media/" not in result["message"]


def test_a_share_that_is_down_costs_the_filing_not_the_document(doc):
    module, _, fake = doc
    fake.open_file = lambda *a, **kw: (_ for _ in ()).throw(OSError("share is down"))
    result = _make(module, "otro.docx")
    assert not result.get("error"), result
    assert result["share_error"]
    # Still delivered, from the workspace copy — which does not survive the
    # container, hence the fallback rather than the normal path.
    assert result["download_link"] == "[📥 Download otro.docx](download:media/otro.docx)"
