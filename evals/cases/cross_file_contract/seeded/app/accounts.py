"""Account lookups."""

from app.store import Store


def find_account(store: Store, account_id: int) -> dict | None:
    """The account with this id, or None when there is none."""
    return store.get("accounts", account_id)
