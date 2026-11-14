"""Display names for account profiles."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    user_id: int
    nickname: str | None


def lookup(profiles: dict[int, Profile], user_id: int) -> Profile | None:
    return profiles.get(user_id)


def display_name(profiles: dict[int, Profile], user_id: int) -> str:
    """The nickname, title-cased, or a placeholder for unknown users and empty nicknames."""
    profile = lookup(profiles, user_id)
    nickname = profile.nickname
    return nickname.title() if nickname else "Anonymous"
