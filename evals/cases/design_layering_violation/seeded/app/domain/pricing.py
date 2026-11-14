"""Price rules."""

from app import storage


def loyalty_discount_percent(years: int) -> int:
    """One percent per year of loyalty, up to ten."""
    return min(years, 10)


def customer_discount_percent(db_path: str, customer_id: int) -> int:
    """The loyalty discount a customer has earned."""
    conn = storage.connect(db_path)
    try:
        return loyalty_discount_percent(storage.loyalty_years(conn, customer_id))
    finally:
        conn.close()
