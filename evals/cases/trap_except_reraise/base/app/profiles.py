"""Load customer profiles from the profile service."""

from typing import Protocol


class ProfileClient(Protocol):
    def get(self, path: str) -> dict[str, str]: ...


def load_profile(client: ProfileClient, user_id: str) -> dict[str, str]:
    """The profile record for a user."""
    return client.get(f"/users/{user_id}")
