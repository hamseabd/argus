"""Tell customers when their order ships."""

from typing import Protocol


class Mailer(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class Orders(Protocol):
    async def mark_notified(self, order_id: int) -> None: ...


SUBJECT = "Your order has shipped"


async def notify_shipped(mailer: Mailer, orders: Orders, order_id: int, email: str) -> None:
    """Email the customer, then record that they were told."""
    body = f"Order {order_id} is on its way."
    mailer.send(email, SUBJECT, body)
    await orders.mark_notified(order_id)
