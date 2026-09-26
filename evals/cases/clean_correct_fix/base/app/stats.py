"""Summary statistics over a list of numbers."""


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
