"""Business rules as pure functions.

Nothing in this package does I/O or imports the storage layer: callers
load what a rule needs and pass it in, so every rule is testable alone.
"""
