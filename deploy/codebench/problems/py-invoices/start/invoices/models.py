from dataclasses import dataclass, field


@dataclass
class Item:
    name: str
    price: float
    qty: int = 1


@dataclass
class Invoice:
    customer: str
    items: list = field(default_factory=list)
    discount: float = 0.0   # a fraction: 0.1 is ten percent off
