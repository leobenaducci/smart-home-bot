TAX_RATE = 0.19


def subtotal(invoice):
    return sum(i.price * i.qty for i in invoice.items)


def after_discount(invoice):
    """The subtotal with the invoice's fractional discount taken off."""
    return subtotal(invoice) * (1 - invoice.discount)


def total(invoice):
    return round(after_discount(invoice) * (1 + TAX_RATE), 2)
