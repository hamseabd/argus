"""Serve uploaded attachments from a fixed directory."""

from pathlib import Path

UPLOAD_ROOT = Path("/srv/uploads")


def read_attachment(name: str) -> bytes:
    """The bytes of an uploaded attachment; name comes from the request URL."""
    path = (UPLOAD_ROOT / name).resolve()
    if not path.is_relative_to(UPLOAD_ROOT):
        raise PermissionError(f"{name} is outside the upload directory")
    return path.read_bytes()
