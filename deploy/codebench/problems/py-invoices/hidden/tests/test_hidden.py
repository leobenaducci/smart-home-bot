import io
import json

from invoices.cli import main
from invoices.models import Invoice, Item
from invoices.pricing import after_discount, subtotal, total


def test_no_discount_is_the_subtotal():
    inv = Invoice("a", [Item("x", 3.0, 2), Item("y", 1.5, 1)])
    assert after_discount(inv) == subtotal(inv) == 7.5


def test_quarter_off():
    inv = Invoice("a", [Item("x", 40.0, 1)], discount=0.25)
    assert after_discount(inv) == 30.0
    assert total(inv) == 35.7


def test_json_with_discount():
    buf = io.StringIO()
    assert main(["Mora", "a:10:2", "--discount", "0.5", "--json"], out=buf) == 0
    data = json.loads(buf.getvalue())
    assert data["customer"] == "Mora"
    assert data["subtotal"] == 20.0
    assert data["total"] == 11.9


def test_text_report_still_text():
    buf = io.StringIO()
    assert main(["Mora", "a:10:2", "--discount", "0.5"], out=buf) == 0
    out = buf.getvalue()
    assert out.startswith("Invoice for Mora")
    assert "Total: 11.90" in out
    assert not out.lstrip().startswith("{")
