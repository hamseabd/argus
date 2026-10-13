"""A tiny in-memory table store."""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, dict[int, dict]] = {}

    def put(self, table: str, key: int, row: dict) -> None:
        self._tables.setdefault(table, {})[key] = row

    def get(self, table: str, key: int) -> dict | None:
        return self._tables.get(table, {}).get(key)
