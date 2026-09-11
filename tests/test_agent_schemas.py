import json

import jsonschema
import pytest
from jsonschema import Draft7Validator

from argus.agent.schemas import output_format, review_schema, verdict_schema
from argus.domain.models import Finding, Review, Verdict


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


def test_review_schema_is_valid_draft7_and_uses_only_draft7_keywords() -> None:
    schema = review_schema()

    Draft7Validator.check_schema(schema)
    assert keywords(schema) <= DRAFT7_KEYWORDS
    assert "$ref" not in keywords(schema)  # inlined so no resolver is needed


def test_verdict_schema_is_valid_draft7() -> None:
    schema = verdict_schema()

    Draft7Validator.check_schema(schema)
    assert keywords(schema) <= DRAFT7_KEYWORDS


def test_review_schema_excludes_pipeline_owned_fields() -> None:
    schema = review_schema()
    finding_props = schema["properties"]["findings"]["items"]["properties"]

    assert "id" not in finding_props
    assert "status" not in finding_props
    assert set(finding_props) == {
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
    assert props["verdict"]["enum"] == ["confirmed", "rejected"]


def test_schemas_are_strict_every_property_required_no_extras() -> None:
    for schema in (review_schema(), verdict_schema()):
        for node in walk(schema):
            if isinstance(node, dict) and node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert sorted(node.get("required", [])) == sorted(node["properties"])


def test_constraints_survive_in_the_schema() -> None:
    schema = review_schema()
    finding = schema["properties"]["findings"]["items"]["properties"]

    assert schema["properties"]["findings"]["maxItems"] == 25
    assert finding["title"]["maxLength"] == 100
    assert finding["line"]["minimum"] == 1
    assert finding["confidence"]["minimum"] == 0
    assert finding["confidence"]["maximum"] == 1
    assert sorted(finding["severity"]["enum"]) == ["critical", "high", "low", "medium"]


def test_prose_fields_have_a_length_floor_in_the_schema_only() -> None:
    # A placeholder that fits the shape ("Test call to diagnose schema validation.")
    # must fail the SDK's validation so the model rewrites it; the domain model
    # itself stays permissive so the pipeline can build a Review from any source.
    assert review_schema()["properties"]["summary"]["minLength"] == 80
    assert verdict_schema()["properties"]["reasoning"]["minLength"] == 80
    assert "minLength" not in Review.model_json_schema()["properties"]["summary"]

    placeholder = {
        "summary": "Test call to diagnose schema validation.",
        "files_reviewed": ["README.md"],
        "findings": [],
    }
    with pytest.raises(jsonschema.ValidationError, match="too short"):
        jsonschema.validate(placeholder, review_schema())


def test_a_review_round_trips_through_the_schema() -> None:
    review = Review(
        summary=(
            "Adds paging to the user list. The slice bound is off by one, so the last "
            "user of every page is dropped; nothing else in the change is affected."
        ),
        files_reviewed=["a.py"],
        findings=[
            Finding(
                id="correctness-1",
                file="a.py",
                line=3,
                severity="high",
                category="correctness",
                title="Off by one",
                description="d",
                evidence="e",
                confidence=0.9,
            )
        ],
    )
    model_output = review.model_dump(exclude={"findings": {"__all__": {"id", "status"}}})

    jsonschema.validate(model_output, review_schema())
    parsed = Review.model_validate(json.loads(json.dumps(model_output)))

    assert parsed.findings[0].id == "correctness-1"
    assert parsed.findings[0].status == "pending"


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
