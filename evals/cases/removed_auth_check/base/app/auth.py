"""Who may do what."""

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    id: int
    roles: frozenset[str]


class Forbidden(Exception):
    pass


def require_role(user: User, role: str) -> None:
    if role not in user.roles:
        raise Forbidden(f"user {user.id} lacks role {role}")
