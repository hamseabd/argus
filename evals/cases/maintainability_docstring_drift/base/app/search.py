"""Searching the product catalogue."""


def search(products: list[str], term: str, limit: int = 10) -> list[str]:
    """Products whose name contains term, case-insensitively.

    Args:
        term: The text to look for.
        limit: The most results to return.
    """
    needle = term.casefold()
    return [p for p in products if needle in p.casefold()][:limit]
