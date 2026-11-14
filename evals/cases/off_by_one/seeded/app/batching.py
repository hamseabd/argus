"""Split work into fixed-size batches."""

from collections.abc import Iterator, Sequence


def batches[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Consecutive slices of items, each at most size long, covering every item."""
    if size < 1:
        raise ValueError("size must be positive")
    last = len(items) - 1
    for start in range(0, last, size):
        yield items[start : start + size]


def batch_count(total: int, size: int) -> int:
    if size < 1:
        raise ValueError("size must be positive")
    return (total + size - 1) // size
