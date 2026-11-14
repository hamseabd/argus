"""Searching the product catalogue."""


def search(products: list[str], term: str, page: int = 1, page_size: int = 10) -> list[str]:
    """One page of the products whose name contains term, case-insensitively.

    Args:
        term: The text to look for.
        limit: The most results to return.
    """
    needle = term.casefold()
    matches = [p for p in products if needle in p.casefold()]
    start = (page - 1) * page_size
    return matches[start : start + page_size]
