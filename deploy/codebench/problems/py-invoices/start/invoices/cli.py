import argparse
import sys

from .models import Invoice, Item
from .report import lines


def parse_item(text):
    name, price, qty = text.split(":")
    return Item(name, float(price), int(qty))


def main(argv=None, out=sys.stdout):
    ap = argparse.ArgumentParser(prog="invoice")
    ap.add_argument("customer")
    ap.add_argument("items", nargs="+", help="name:price:qty")
    ap.add_argument("--discount", type=float, default=0.0)
    args = ap.parse_args(argv)
    inv = Invoice(args.customer, [parse_item(t) for t in args.items], args.discount)
    for ln in lines(inv):
        print(ln, file=out)
    return 0
