import argparse
import json
import sys

from .models import Invoice, Item
from .pricing import subtotal, total
from .report import lines


def parse_item(text):
    name, price, qty = text.split(":")
    return Item(name, float(price), int(qty))


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="invoice")
    ap.add_argument("customer")
    ap.add_argument("items", nargs="+", help="name:price:qty")
    ap.add_argument("--discount", type=float, default=0.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    inv = Invoice(args.customer, [parse_item(t) for t in args.items], args.discount)
    if args.json:
        print(json.dumps({"customer": inv.customer, "subtotal": subtotal(inv), "total": total(inv),
                          "items": [{"name": i.name, "price": i.price, "qty": i.qty} for i in inv.items]}),
              file=out)
        return 0
    for ln in lines(inv):
        print(ln, file=out)
    return 0
