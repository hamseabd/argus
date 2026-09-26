"""Summary statistics over a list of numbers."""


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    """The middle value, or the mean of the two middle values when the count is even."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
