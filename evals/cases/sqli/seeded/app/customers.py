"""Customer lookups against a SQLite connection."""

import sqlite3


def find_by_email(conn: sqlite3.Connection, email: str) -> tuple | None:
    """Return the (id, name, email) row for a customer, or None."""
    cursor = conn.execute("SELECT id, name, email FROM customers WHERE email = ?", (email,))
    return cursor.fetchone()


def search_by_name(conn: sqlite3.Connection, fragment: str) -> list[tuple]:
    """Customers whose name contains fragment, in name order."""
    query = f"SELECT id, name, email FROM customers WHERE name LIKE '%{fragment}%' ORDER BY name"
    return conn.execute(query).fetchall()


def count_customers(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
