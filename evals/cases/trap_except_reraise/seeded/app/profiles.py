"""Load customer profiles from the profile service."""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class ProfileClient(Protocol):
    def get(self, path: str) -> dict[str, str]: ...


def load_profile(client: ProfileClient, user_id: str) -> dict[str, str]:
    """The profile record for a user."""
    try:
        return client.get(f"/users/{user_id}")
    except Exception:
        logger.exception("profile fetch failed", extra={"user_id": user_id})
        raise
