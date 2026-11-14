"""Login sessions."""

from datetime import UTC, datetime, timedelta

SESSION_LIFETIME = timedelta(minutes=30)
"""How long a session stays valid after it is created."""


def expires_at(created: datetime) -> datetime:
    """When a session created at created stops being valid."""
    return created.astimezone(UTC) + SESSION_LIFETIME
