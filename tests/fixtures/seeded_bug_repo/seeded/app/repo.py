"""User lookups against a SQLite connection."""

import sqlite3


def find_user(conn: sqlite3.Connection, name: str) -> tuple | None:
    """Return the (id, name, email) row for a user, or None."""
    cursor = conn.execute(f"SELECT id, name, email FROM users WHERE name = '{name}'")
    return cursor.fetchone()


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
