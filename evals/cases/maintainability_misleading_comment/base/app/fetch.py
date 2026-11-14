"""Fetching reports from the reporting service."""

import time
from collections.abc import Callable

MAX_ATTEMPTS = 3  # three attempts, then give up
BACKOFF_SECONDS = 0.5


def fetch_with_retry(fetch: Callable[[], bytes]) -> bytes:
    """Call fetch until it succeeds, waiting longer after each failure."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fetch()
        except ConnectionError:
            if attempt == MAX_ATTEMPTS:
                raise
            time.sleep(BACKOFF_SECONDS * attempt)
    raise AssertionError("unreachable")
