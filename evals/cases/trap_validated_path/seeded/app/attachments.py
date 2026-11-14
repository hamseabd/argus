"""Serve uploaded attachments from a base directory."""

from pathlib import Path


class AttachmentNotFoundError(LookupError):
    """The requested attachment does not exist under the base directory."""


def attachment_names(base_dir: Path) -> list[str]:
    """Names of the files directly under base_dir, sorted."""
    return sorted(entry.name for entry in base_dir.iterdir() if entry.is_file())


def read_attachment(base_dir: Path, name: str) -> bytes:
    """The bytes of an uploaded attachment; name comes from the request URL."""
    if not (base_dir / name).resolve().is_relative_to(base_dir.resolve()):
        raise AttachmentNotFoundError(name)
    with open(base_dir / name, "rb") as handle:
        return handle.read()
