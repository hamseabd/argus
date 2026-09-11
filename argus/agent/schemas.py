"""Structured-output schemas for the lead reviewer and the verifier.

The schemas are derived from the domain models so the two cannot drift,
then reshaped for the SDK's validator: pipeline-owned fields are removed,
$ref definitions are inlined, every property is required (optional ones are
nullable instead), objects forbid extra keys, and only draft-07 keywords
remain.
"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from argus.domain.models import Review, Verdict

_PIPELINE_OWNED: dict[type[BaseModel], set[str]] = {
    Review: set(),
    Verdict: {"finding_id"},
}
_FINDING_PIPELINE_OWNED = {"id", "status"}
_DROPPED_KEYWORDS = {"default", "title"}


def review_schema() -> dict[str, Any]:
    schema = _strict(Review.model_json_schema())
    finding = schema["properties"]["findings"]["items"]
    _remove_properties(finding, _FINDING_PIPELINE_OWNED)
    return schema


def verdict_schema() -> dict[str, Any]:
    schema = _strict(Verdict.model_json_schema())
    _remove_properties(schema, _PIPELINE_OWNED[Verdict])
    return schema


def output_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap a schema the way ClaudeAgentOptions.output_format expects."""
    return {"type": "json_schema", "schema": schema}


def _strict(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.pop("$defs", {})
    return _rewrite(deepcopy(schema), definitions)


def _rewrite(node: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_rewrite(item, definitions) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        return _rewrite(deepcopy(definitions[name]), definitions)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROPPED_KEYWORDS:
            continue
        if key == "properties":
            # Keys here are field names, not keywords, so none are dropped.
            out[key] = {name: _rewrite(sub, definitions) for name, sub in value.items()}
        else:
            out[key] = _rewrite(value, definitions)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def _remove_properties(obj: dict[str, Any], names: set[str]) -> None:
    for name in names:
        obj["properties"].pop(name, None)
    obj["required"] = [n for n in obj["required"] if n not in names]
