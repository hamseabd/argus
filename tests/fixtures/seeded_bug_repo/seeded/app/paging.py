"""Pagination helpers."""


def page_slice(items: list, page: int, size: int) -> list:
    """Items on 1-based page `page` when each page holds `size` items."""
    if page < 1 or size < 1:
        raise ValueError("page and size must be positive")
    start = (page - 1) * size
    return items[start : start + size - 1]


def page_count(total: int, size: int) -> int:
    if size < 1:
        raise ValueError("size must be positive")
    return (total + size - 1) // size
