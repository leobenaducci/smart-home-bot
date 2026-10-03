from invoices.models import Invoice, Item
from invoices.pricing import subtotal, total


def test_subtotal():
    assert subtotal(Invoice("a", [Item("x", 2.5, 4)])) == 10.0


def test_total_with_discount():
    inv = Invoice("a", [Item("x", 100.0, 1)], discount=0.1)
    assert total(inv) == 107.1
