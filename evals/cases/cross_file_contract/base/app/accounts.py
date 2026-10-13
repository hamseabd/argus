"""Account lookups."""

from app.store import Store


class AccountNotFound(LookupError):
    """No account has that id."""


def find_account(store: Store, account_id: int) -> dict:
    """The account with this id; raises AccountNotFound when there is none."""
    row = store.get("accounts", account_id)
    if row is None:
        raise AccountNotFound(account_id)
    return row
