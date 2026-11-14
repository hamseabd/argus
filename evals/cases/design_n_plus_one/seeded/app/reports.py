"""Reports over orders."""

from dataclasses import dataclass

from app.store import CustomerStore


@dataclass(frozen=True)
class Order:
    id: int
    customer_id: int
    total_cents: int


def order_lines(store: CustomerStore, orders: list[Order]) -> list[str]:
    """One report line per order: its id, its customer's name, and its total."""
    lines = []
    for order in orders:
        customer = store.get(order.customer_id)
        lines.append(f"{order.id} {customer.name} {order.total_cents}")
    return lines
