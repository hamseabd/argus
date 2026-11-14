"""Serve uploaded attachments from a base directory."""

from pathlib import Path


class AttachmentNotFoundError(LookupError):
    """The requested attachment does not exist under the base directory."""


def attachment_names(base_dir: Path) -> list[str]:
    """Names of the files directly under base_dir, sorted."""
    return sorted(entry.name for entry in base_dir.iterdir() if entry.is_file())
