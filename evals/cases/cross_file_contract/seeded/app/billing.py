"""Monthly billing."""

from app.accounts import find_account
from app.store import Store


def monthly_charge(store: Store, account_id: int) -> int:
    """The charge in cents for an account's plan this month, less any credit."""
    account = find_account(store, account_id)
    return account["plan_cents"] - account.get("credit_cents", 0)
