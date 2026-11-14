"""The storage layer: the only module that talks to the database."""

import sqlite3


def connect(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path)


def loyalty_years(conn: sqlite3.Connection, customer_id: int) -> int:
    row = conn.execute(
        "SELECT years FROM loyalty WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    return row[0] if row else 0
