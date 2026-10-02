"""Serve uploaded attachments from a fixed directory."""

import os

UPLOAD_ROOT = "/srv/uploads"


def read_attachment(name: str) -> bytes:
    """The bytes of an uploaded attachment; name comes from the request URL."""
    path = os.path.join(UPLOAD_ROOT, name)
    with open(path, "rb") as handle:
        return handle.read()
