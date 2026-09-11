"""Structured logging for the whole run.

One structlog configuration, JSON lines on a non-TTY stream or when asked,
a console renderer on a TTY. Run-scoped fields (run_id, then session_id
once known) are bound in a context variable so every event from every
module carries them without threading a logger through the call stack.
"""

import logging
import secrets
import sys
from typing import IO, Literal

import structlog

LogFormat = Literal["auto", "json", "console"]
ResolvedFormat = Literal["json", "console"]
LogLevel = Literal["debug", "info", "warning"]
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


def new_run_id() -> str:
    return secrets.token_hex(6)


def resolve_format(log_format: LogFormat, stream: IO[str]) -> ResolvedFormat:
    if log_format == "auto":
        return "console" if stream.isatty() else "json"
    return log_format


def configure(
    log_format: LogFormat = "auto", stream: IO[str] | None = None, level: LogLevel = "info"
) -> None:
    """Configure structlog for the process; safe to call more than once."""
    out = sys.stderr if stream is None else stream
    renderer: structlog.typing.Processor
    if resolve_format(log_format, out) == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    structlog.contextvars.clear_contextvars()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level]),
        logger_factory=structlog.PrintLoggerFactory(file=out),
        cache_logger_on_first_use=False,
    )


def bind_run(**fields: str) -> None:
    """Attach run-scoped fields (run_id, session_id) to every later event."""
    structlog.contextvars.bind_contextvars(**fields)


def get_logger() -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger()
