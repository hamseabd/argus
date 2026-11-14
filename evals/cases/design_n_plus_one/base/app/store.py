"""Access to the customer store, a remote service."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Customer:
    id: int
    name: str


class CustomerStore:
    """A client for the customer service; every method is one network round trip."""

    def __init__(self, customers: dict[int, Customer]) -> None:
        self._customers = customers

    def get(self, customer_id: int) -> Customer:
        """One customer by id."""
        return self._customers[customer_id]

    def get_many(self, customer_ids: list[int]) -> dict[int, Customer]:
        """Many customers by id, in one request."""
        return {i: self._customers[i] for i in customer_ids}
