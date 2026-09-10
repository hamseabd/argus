"""Where Claude credentials come from.

Argus never reads credentials itself; the Claude Agent SDK does.
This module only reports which environment variable will supply them,
so the CLI can fail early with a useful message and the smoke script can
log which path it exercised.
"""

import os
from collections.abc import Mapping

CREDENTIAL_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


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
