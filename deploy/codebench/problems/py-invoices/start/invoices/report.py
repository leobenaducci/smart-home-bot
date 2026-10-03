from .pricing import subtotal, total


def lines(invoice):
    out = [f"Invoice for {invoice.customer}"]
    for i in invoice.items:
        out.append(f"  {i.name} x{i.qty}: {i.price * i.qty:.2f}")
    out.append(f"Subtotal: {subtotal(invoice):.2f}")
    out.append(f"Total: {total(invoice):.2f}")
    return out
