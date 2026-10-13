import json

import jsonschema
import pytest
from jsonschema import Draft7Validator

from argus.agent.schemas import (
    finding_input_schema,
    output_format,
    triage_schema,
    verdict_schema,
)
from argus.domain.models import Finding, Triage, Verdict


def walk(node):
    """Yield every schema node; the values of "properties" are nodes, its keys are names."""
    yield node
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties":
                for sub in value.values():
                    yield from walk(sub)
            else:
                yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def keywords(schema: dict) -> set[str]:
    return {k for node in walk(schema) if isinstance(node, dict) for k in node}


DRAFT7_KEYWORDS = {
    "$schema",
    "$id",
    "$ref",
    "$comment",
    "title",
    "description",
    "default",
    "examples",
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "definitions",
    "format",
}


FINDING_INPUT = {
    "file": "a.py",
    "line": 3,
    "end_line": None,
    "severity": "high",
    "category": "correctness",
    "title": "Off by one",
    "description": "The slice stops one short of the page.",
    "evidence": "users[start:end - 1] drops the last user.",
    "suggested_fix": None,
    "confidence": 0.9,
}


@pytest.mark.parametrize("schema", [triage_schema(), finding_input_schema(), verdict_schema()])
def test_schemas_are_valid_draft7_and_use_only_draft7_keywords(schema: dict) -> None:
    Draft7Validator.check_schema(schema)
    assert keywords(schema) <= DRAFT7_KEYWORDS
    assert "$ref" not in keywords(schema)  # inlined so no resolver is needed


def test_the_finding_input_excludes_pipeline_owned_fields() -> None:
    assert set(finding_input_schema()["properties"]) == {
        "file",
        "line",
        "end_line",
        "severity",
        "category",
        "title",
        "description",
        "evidence",
        "suggested_fix",
        "confidence",
    }


def test_verdict_schema_excludes_finding_id() -> None:
    props = verdict_schema()["properties"]

    assert set(props) == {"verdict", "reasoning", "confidence"}
    assert props["verdict"]["enum"] == ["confirmed", "rejected", "inconclusive"]


def test_the_triage_names_drops_by_id_and_reason() -> None:
    props = triage_schema()["properties"]

    assert set(props) == {"summary", "files_reviewed", "dropped"}
    assert set(props["dropped"]["items"]["properties"]) == {"id", "reason"}


def test_schemas_are_strict_every_property_required_no_extras() -> None:
    for schema in (triage_schema(), finding_input_schema(), verdict_schema()):
        for node in walk(schema):
            if isinstance(node, dict) and node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert sorted(node.get("required", [])) == sorted(node["properties"])


def test_constraints_survive_in_the_schema() -> None:
    finding = finding_input_schema()["properties"]

    assert finding["title"]["maxLength"] == 100
    assert finding["line"]["minimum"] == 1
    assert finding["confidence"]["minimum"] == 0
    assert finding["confidence"]["maximum"] == 1
    assert sorted(finding["severity"]["enum"]) == ["critical", "high", "low", "medium"]


def test_prose_fields_have_a_length_floor_in_the_schema_only() -> None:
    # A placeholder that fits the shape ("Test call to diagnose schema validation.")
    # must fail validation so the model rewrites it; the domain model itself stays
    # permissive so the pipeline can build a Review from any source.
    assert triage_schema()["properties"]["summary"]["minLength"] == 80
    assert verdict_schema()["properties"]["reasoning"]["minLength"] == 80
    assert finding_input_schema()["properties"]["evidence"]["minLength"] == 20
    assert "minLength" not in Triage.model_json_schema()["properties"]["summary"]

    placeholder = {
        "summary": "Test call to diagnose schema validation.",
        "files_reviewed": ["README.md"],
        "dropped": [],
    }
    with pytest.raises(jsonschema.ValidationError, match="too short"):
        jsonschema.validate(placeholder, triage_schema())


def test_a_triage_round_trips_through_the_schema() -> None:
    model_output = {
        "summary": (
            "Adds paging to the user list. The slice bound is off by one, so the last "
            "user of every page is dropped; nothing else in the change is affected."
        ),
        "files_reviewed": ["a.py"],
        "dropped": [{"id": "F2", "reason": "duplicate of F1"}],
    }

    jsonschema.validate(model_output, triage_schema())
    assert Triage.model_validate(json.loads(json.dumps(model_output))).dropped_ids == {"F2"}


def test_a_finding_input_round_trips_through_the_schema_into_a_finding() -> None:
    jsonschema.validate(FINDING_INPUT, finding_input_schema())
    parsed = Finding.model_validate({**FINDING_INPUT, "id": "F1"})

    assert (parsed.status, parsed.reported_by) == ("pending", None)


def test_a_verdict_round_trips_through_the_schema() -> None:
    model_output = {
        "verdict": "rejected",
        "reasoning": (
            "paging.py:7 clamps `size` to the list length before slicing, so the "
            "reported overflow cannot happen. The finding describes the old code path."
        ),
        "confidence": 0.4,
    }

    jsonschema.validate(model_output, verdict_schema())
    assert Verdict(finding_id="x", **model_output).verdict == "rejected"


def test_output_format_wraps_a_schema_the_sdk_way() -> None:
    fmt = output_format(verdict_schema())

    assert fmt == {"type": "json_schema", "schema": verdict_schema()}
