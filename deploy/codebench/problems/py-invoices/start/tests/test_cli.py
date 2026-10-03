import io
import json

from invoices.cli import main


def test_text_report():
    buf = io.StringIO()
    assert main(["Tomi", "pen:2:3"], out=buf) == 0
    assert "Invoice for Tomi" in buf.getvalue()


def test_json_report():
    buf = io.StringIO()
    assert main(["Tomi", "pen:2:3", "book:10:1", "--json"], out=buf) == 0
    data = json.loads(buf.getvalue())
    assert data["customer"] == "Tomi"
    assert data["subtotal"] == 16.0
    assert data["total"] == 19.04
    assert [i["name"] for i in data["items"]] == ["pen", "book"]
