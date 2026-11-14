"""The application's error types; the API layer maps each to a status code."""


class AppError(Exception):
    """Base class for errors the API reports to the caller."""


class NotFound(AppError):
    """The requested resource does not exist."""


class InvalidInput(AppError):
    """The request is well formed but its values are not acceptable."""
