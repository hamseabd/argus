"""Project administration handlers."""

from app.auth import User, require_role


class ProjectStore:
    def __init__(self) -> None:
        self._projects: dict[int, str] = {}

    def rename(self, project_id: int, name: str) -> None:
        self._projects[project_id] = name

    def delete(self, project_id: int) -> None:
        self._projects.pop(project_id, None)


def rename_project(store: ProjectStore, user: User, project_id: int, name: str) -> None:
    require_role(user, "editor")
    store.rename(project_id, name.strip())


def delete_project(store: ProjectStore, user: User, project_id: int) -> None:
    require_role(user, "admin")
    store.delete(project_id)
