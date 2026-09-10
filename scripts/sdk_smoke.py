"""Run one real query through the Claude Agent SDK and report what it cost.

This is the go/no-go check for Argus. It proves that the SDK runs headless
with whatever credentials the environment supplies (the subscription token
in CI), that the model id is accepted, and that cost and usage are reported.

Usage:
    uv run python scripts/sdk_smoke.py --model claude-sonnet-5

Prints one JSON line to stdout. Exit code 0 only when the result subtype is
"success" and the model answered with the expected token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from argus.auth import credential_source

MAGIC = "ARGUS_OK"


async def run(model: str, max_budget_usd: float) -> int:
    source = credential_source()
    print(f"credential source: {source or 'none in env (machine login)'}", file=sys.stderr)
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=f"You are a smoke test. Reply with exactly {MAGIC} and nothing else.",
        allowed_tools=[],
        permission_mode="dontAsk",
        max_turns=1,
        max_budget_usd=max_budget_usd,
        stderr=lambda line: print(f"[cli] {line}", file=sys.stderr),
    )
    result: ResultMessage | None = None
    try:
        # The result message is always last. Do not return or break inside the
        # loop: closing the query generator early raises from its aclose().
        async for message in query(prompt="Reply now.", options=options):
            if isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        print(json.dumps({"ok": False, "model": model, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    if result is None:
        print(json.dumps({"ok": False, "model": model, "subtype": "no_result"}))
        return 1
    ok = result.subtype == "success" and MAGIC in (result.result or "")
    print(
        json.dumps(
            {
                "ok": ok,
                "model": model,
                "subtype": result.subtype,
                "cost_usd": result.total_cost_usd,
                "num_turns": result.num_turns,
                "duration_ms": result.duration_ms,
                "input_tokens": (result.usage or {}).get("input_tokens"),
                "cache_creation_input_tokens": (result.usage or {}).get(
                    "cache_creation_input_tokens"
                ),
                "output_tokens": (result.usage or {}).get("output_tokens"),
                "session_id": result.session_id,
            }
        )
    )
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default="claude-sonnet-5", help="model id to exercise")
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=1.0,
        help=(
            "abort if the run would exceed this. A cold-cache one-turn Opus reply costs "
            "about $0.37 because the runtime sends ~25K tokens of tool definitions, so "
            "the default must clear that."
        ),
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.model, args.max_budget_usd)))


if __name__ == "__main__":
    main()
