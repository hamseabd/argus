"""Typed exceptions raised by the Argus pipeline.

Every error derives from ArgusError so the CLI can catch one type, print a
message, and choose an exit code. Errors carry the facts a caller needs
(cost so far, HTTP status) as attributes rather than only in the message.
"""

_BODY_PREVIEW_CHARS = 400


class ArgusError(Exception):
    """Base class for every error Argus raises on purpose."""


class AgentRunError(ArgusError):
    """A query ended with a non-success result subtype."""

    def __init__(self, subtype: str, cost_usd: float, session_id: str | None = None) -> None:
        self.subtype = subtype
        self.cost_usd = cost_usd
        self.session_id = session_id
        super().__init__(f"agent run ended with {subtype} after ${cost_usd:.2f}")


class ReviewProtocolError(ArgusError):
    """A query succeeded but did not return the structured output it promised.

    The query was still paid for, so the cost travels with the error.
    """

    def __init__(self, message: str, cost_usd: float = 0.0, session_id: str | None = None) -> None:
        self.cost_usd = cost_usd
        self.session_id = session_id
        super().__init__(message)


class GitHubError(ArgusError):
    """The GitHub API answered with a non-2xx status, or could not be reached (status None)."""

    def __init__(self, status: int | None, body: str) -> None:
        self.status = status
        self.body = body
        preview = body if len(body) <= _BODY_PREVIEW_CHARS else body[:_BODY_PREVIEW_CHARS] + "..."
        if status is None:
            super().__init__(f"GitHub request failed: {preview}")
        else:
            super().__init__(f"GitHub responded {status}: {preview}")


class GitError(ArgusError):
    """A git subprocess failed or the repository is not in the expected state."""


class DiffParseError(ArgusError):
    """A unified diff did not have the shape the parser expects."""
