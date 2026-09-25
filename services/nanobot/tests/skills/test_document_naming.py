"""What a document is called when the model does not say.

Every document was `documento.<ext>`: the second overwrote the first, and a
household's folder read as one file saved over and over. The Designer's chat on
2026-09-03 has both `documento.html` and `documento.pdf` from a single turn.

The name is derived from the document itself now. `filename` is still the
model's to set and still wins -- what is pinned here is the fallback, because
it has to work on the turn the field is forgotten, which is the only turn it
is ever needed on.
"""

import importlib.util
import pathlib

import pytest

_PATH = (pathlib.Path(__file__).resolve().parents[2]
         / "nanobot" / "skills" / "document" / "create_doc.py")
_spec = importlib.util.spec_from_file_location("create_doc", _PATH)
create_doc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_doc)

name_of = create_doc._resolve_format


class TestTheModelStillWins:
    def test_a_given_filename_is_kept(self):
        assert name_of({"format": "pdf", "filename": "informe.pdf",
                        "title": "Otra cosa"}) == ("pdf", "informe.pdf")

    def test_and_its_extension_is_corrected_to_the_format(self):
        fmt, name = name_of({"format": "pdf", "filename": "informe.docx",
                             "title": "Otra cosa"})
        assert (fmt, name) == ("pdf", "informe.pdf")


class TestDerivedFromContent:
    def test_from_the_title(self):
        assert name_of({"format": "pdf", "title": "Presupuesto de la cocina"}) \
            == ("pdf", "presupuesto-de-la-cocina.pdf")

    def test_from_the_first_heading_when_there_is_no_title(self):
        fmt, name = name_of({"format": "html", "sections": [
            {"heading": "Plan de obra"}, {"heading": "Costos"}]})
        assert name == "plan-de-obra.html"

    def test_from_a_raw_html_page_title(self):
        fmt, name = name_of({"format": "html",
                             "html": "<html><head><title>Menú semanal</title>"
                                     "</head><body>x</body></html>"})
        assert name == "menu-semanal.html"

    def test_from_an_h1_when_the_page_has_no_title(self):
        fmt, name = name_of({"format": "html",
                             "html": "<body><h1>Lista de <b>compras</b></h1></body>"})
        assert name == "lista-de-compras.html"

    def test_from_the_first_paragraph_as_a_last_resort(self):
        fmt, name = name_of({"format": "pdf", "sections": [
            {"text": "Resumen de la reunión del martes"}]})
        assert name == "resumen-de-la-reunion-del-martes.pdf"

    def test_it_falls_back_to_documento_when_there_is_nothing(self):
        assert name_of({"format": "pdf"}) == ("pdf", "documento.pdf")

    def test_an_unusable_title_does_not_win_over_a_usable_heading(self):
        """A title of punctuation slugs to nothing and must not be chosen."""
        fmt, name = name_of({"format": "pdf", "title": "¿? — ...",
                             "sections": [{"heading": "Cuentas de agosto"}]})
        assert name == "cuentas-de-agosto.pdf"


class TestTheSlug:
    @pytest.mark.parametrize("text,expected", [
        ("Presupuesto de la cocina", "presupuesto-de-la-cocina"),
        ("Menú semanal", "menu-semanal"),
        ("Informe anual — 2026", "informe-anual-2026"),
        ("  espacios   raros  ", "espacios-raros"),
        ("Factura (1).pdf", "factura-1-pdf"),
        ("¿?¡!", ""),
        ("", ""),
    ])
    def test_slugs(self, text, expected):
        assert create_doc._slug(text) == expected

    def test_a_long_title_is_cut_on_a_word(self):
        slug = create_doc._slug("palabra " * 30)
        assert len(slug) <= 60
        assert not slug.endswith("-")
        assert slug.split("-")[-1] == "palabra"

    def test_the_name_can_never_walk_out_of_the_folder(self):
        assert "/" not in create_doc._slug("../../etc/passwd")
        assert create_doc._slug("../../etc/passwd") == "etc-passwd"


class TestTwoDocumentsInOneTurn:
    def test_the_html_and_the_pdf_share_a_stem(self):
        """The Designer sends the same payload twice, once per format."""
        data = {"title": "Presupuesto de la cocina"}
        _, html = name_of(dict(data, format="html"))
        _, pdf = name_of(dict(data, format="pdf"))
        assert (html, pdf) == ("presupuesto-de-la-cocina.html",
                               "presupuesto-de-la-cocina.pdf")

    def test_and_neither_is_documento(self):
        data = {"title": "Presupuesto de la cocina"}
        assert not any(name_of(dict(data, format=f))[1].startswith("documento")
                       for f in ("html", "pdf"))
