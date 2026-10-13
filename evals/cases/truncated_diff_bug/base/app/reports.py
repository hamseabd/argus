"""Summary statistics for the monthly sales report."""


def total(rows):
    """Sum of every row's amount."""
    return sum(row["amount"] for row in rows)


def average(rows):
    """Mean amount per row; 0.0 for a month with no sales."""
    if not rows:
        return 0.0
    return total(rows) / len(rows)


def largest(rows):
    """The row with the largest amount, or None for an empty month."""
    return max(rows, key=lambda row: row["amount"], default=None)


def smallest(rows):
    """The row with the smallest amount, or None for an empty month."""
    return min(rows, key=lambda row: row["amount"], default=None)


def by_region(rows):
    """Rows grouped by their region code."""
    groups = {}
    for row in rows:
        groups.setdefault(row["region"], []).append(row)
    return groups


def region_totals(rows):
    """Total amount per region."""
    return {region: total(group) for region, group in by_region(rows).items()}


def top_regions(rows, count):
    """The count regions with the highest totals, highest first."""
    totals = region_totals(rows)
    return sorted(totals, key=totals.get, reverse=True)[:count]


def share(rows, region):
    """A region's share of the month's total, between 0 and 1."""
    overall = total(rows)
    if overall == 0:
        return 0.0
    return region_totals(rows).get(region, 0) / overall


def format_cents(amount):
    """An amount in cents as a dollar string."""
    return f"${amount / 100:,.2f}"


def summary_line(rows):
    """One line for the report header."""
    return f"{len(rows)} sales, {format_cents(total(rows))} total, {format_cents(average(rows))} average"
