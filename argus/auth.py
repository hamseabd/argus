"""Where Claude credentials come from.

Argus never reads credentials itself; the Claude Agent SDK does.
This module only reports which environment variable will supply them and
whether the value looks usable, so the CLI can fail early with a useful
message and the smoke script can log which path it exercised.
"""

import os
from collections.abc import Mapping

OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY_VAR = "ANTHROPIC_API_KEY"
CREDENTIAL_ENV_VARS: tuple[str, ...] = (OAUTH_TOKEN_VAR, API_KEY_VAR)
OAUTH_TOKEN_PREFIX = "sk-ant-oat"
MIN_CREDENTIAL_LENGTH = 60


def credential_source(env: Mapping[str, str] | None = None) -> str | None:
    """Return the first credential variable with a non-blank value, or None.

    Order matters: the subscription token is checked first because it is the
    only path that keeps the project free to run.
    """
    source = os.environ if env is None else env
    for name in CREDENTIAL_ENV_VARS:
        if source.get(name, "").strip():
            return name
    return None


def credential_problem(env: Mapping[str, str] | None = None) -> str | None:
    """Describe what is wrong with the configured credential, or None if it looks usable.

    The checks only look at shape: presence, whitespace, prefix, and length.
    They catch the common copy-and-paste failures (a truncated or line-broken
    token) before a request is made, and they never reveal the value itself.
    """
    source = os.environ if env is None else env
    name = credential_source(source)
    if name is None:
        return f"no credential set; expected {' or '.join(CREDENTIAL_ENV_VARS)}"
    value = source[name]
    if value != value.strip():
        return f"{name} has leading or trailing whitespace"
    if any(char.isspace() for char in value):
        return f"{name} contains whitespace inside the value"
    if name == OAUTH_TOKEN_VAR and not value.startswith(OAUTH_TOKEN_PREFIX):
        return f"{name} should start with {OAUTH_TOKEN_PREFIX}"
    if len(value) < MIN_CREDENTIAL_LENGTH:
        return (
            f"{name} is {len(value)} characters; a full token is at least {MIN_CREDENTIAL_LENGTH}"
        )
    return None
