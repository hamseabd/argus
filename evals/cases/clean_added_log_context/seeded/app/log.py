"""Structured logging for the service."""

import logging


def get_logger() -> logging.Logger:
    return logging.getLogger("app")
