"""Login sessions."""

from datetime import UTC, datetime, timedelta


def expires_at(created: datetime) -> datetime:
    """When a session created at created stops being valid."""
    return created.astimezone(UTC) + timedelta(seconds=1800)
