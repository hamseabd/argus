"""Findings are recorded through a tool, validated one at a time, so no agent re-types them."""

import asyncio
import json
from pathlib import Path

import pytest
from mcp.client import Client

from argus.agent.findings import MAX_RECORDED, REPORT_TOOL, FindingCollector, report_finding_tool
from argus.agent.tools import REPORT_FINDING_TOOL_NAME, argus_tools, build_argus_server

RAW = {
    "file": "app/repo.py",
    "line": 3,
    "end_line": 4,
    "severity": "high",
    "category": "security",
    "title": "SQL built from user input",
    "description": "The name is interpolated into the SQL text.",
    "evidence": "search() passes the request's name straight into execute().",
    "suggested_fix": "Use a bound parameter.",
    "confidence": 0.9,
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "repo.py").write_text("a\nb\nc\nd\ne\n")
    return tmp_path


def test_a_valid_finding_is_recorded_under_a_fresh_id(repo: Path) -> None:
    collector = FindingCollector(repo)

    assert collector.add(RAW) == "F1"
    assert collector.add({**RAW, "line": 5, "end_line": None}) == "F2"
    assert [f.id for f in collector.findings()] == ["F1", "F2"]
    assert collector.findings()[0].title == RAW["title"]


def test_an_invalid_finding_is_refused_with_a_reason_the_model_can_fix(repo: Path) -> None:
    collector = FindingCollector(repo)

    with pytest.raises(ValueError, match="title"):
        collector.add({**RAW, "title": "x" * 101})
    with pytest.raises(ValueError, match="severity"):
        collector.add({**RAW, "severity": "blocker"})
    assert collector.findings() == []


def test_a_finding_on_a_file_that_does_not_exist_is_refused(repo: Path) -> None:
    with pytest.raises(ValueError, match=r"app/nope\.py does not exist"):
        FindingCollector(repo).add({**RAW, "file": "app/nope.py"})


def test_a_finding_past_the_end_of_its_file_is_refused(repo: Path) -> None:
    with pytest.raises(ValueError, match="has 5 lines"):
        FindingCollector(repo).add({**RAW, "line": 9, "end_line": None})


def test_a_finding_outside_the_repository_is_refused(repo: Path) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        FindingCollector(repo).add({**RAW, "file": "../etc/passwd"})


def test_pipeline_owned_fields_cannot_be_set_by_the_reporter(repo: Path) -> None:
    with pytest.raises(ValueError, match="status"):
        FindingCollector(repo).add({**RAW, "status": "confirmed"})


def test_recording_stops_at_the_cap(repo: Path) -> None:
    collector = FindingCollector(repo)
    for _ in range(MAX_RECORDED):
        collector.add(RAW)

    with pytest.raises(ValueError, match="already recorded"):
        collector.add(RAW)


def test_findings_carry_the_agent_each_id_is_attributed_to(repo: Path) -> None:
    collector = FindingCollector(repo)
    collector.add(RAW)
    collector.add(RAW)

    collector.attribute("F1", "security")

    assert [f.reported_by for f in collector.findings()] == ["security", None]


def test_the_tool_answers_with_the_id_or_with_the_error(repo: Path) -> None:
    collector = FindingCollector(repo)
    handler = report_finding_tool(collector).handler

    ok = asyncio.run(handler(RAW))
    bad = asyncio.run(handler({**RAW, "confidence": 3}))

    assert "is_error" not in ok
    assert ok["content"][0]["text"].startswith("Recorded F1")
    assert bad["is_error"] is True
    assert "confidence" in bad["content"][0]["text"]


def test_the_tool_schema_is_the_finding_minus_what_the_pipeline_owns(repo: Path) -> None:
    schema = report_finding_tool(FindingCollector(repo)).input_schema

    assert schema["type"] == "object"
    assert {"id", "status", "reported_by"}.isdisjoint(schema["properties"])
    assert {"file", "line", "severity", "category", "title"} <= set(schema["required"])
    json.dumps(schema)  # serialisable for the wire


def test_the_report_tool_is_served_only_with_a_collector(repo: Path) -> None:
    assert [t.name for t in argus_tools(repo)] == ["git_history"]
    assert [t.name for t in argus_tools(repo, FindingCollector(repo))] == [
        "git_history",
        "report_finding",
    ]
    assert REPORT_FINDING_TOOL_NAME == "mcp__argus__report_finding"


def test_a_placeholder_finding_is_refused_like_the_schema_would(repo: Path) -> None:
    # The CLI may not validate an MCP tool's input against its schema, so the
    # collector applies the same length floor itself.
    with pytest.raises(ValueError, match="evidence"):
        FindingCollector(repo).add({**RAW, "evidence": "see code"})


def test_a_finding_in_the_git_directory_is_refused(repo: Path) -> None:
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "app" / "cfg").symlink_to(repo / ".git" / "config")

    for path in (".git/config", ".GIT/config", "app/cfg"):
        with pytest.raises(ValueError, match=r"outside the repository or inside its \.git"):
            FindingCollector(repo).add({**RAW, "file": path, "line": 1, "end_line": None})


def test_counting_lines_stops_once_the_finding_is_known_to_fit(repo: Path) -> None:
    big = repo / "app" / "big.py"
    big.write_text("x\n" * 100_000)

    assert FindingCollector(repo).add({**RAW, "file": "app/big.py", "line": 3, "end_line": 4})


def complete(**changes: object) -> dict:
    """RAW with every field the wire schema requires, the nullable ones given as null."""
    return {**RAW, "replacement": None, **changes}


def without(field: str, raw: dict) -> dict:
    return {k: v for k, v in raw.items() if k != field}


def call_report_tool(repo: Path, collector: FindingCollector, args: dict) -> tuple[bool, str]:
    """Call report_finding through the served MCP server, the way the CLI does."""

    async def call() -> tuple[bool, str]:
        server = build_argus_server(repo, collector)["instance"]
        async with Client(server) as client:
            result = await client.call_tool(REPORT_TOOL, args)
        return result.is_error, result.content[0].text

    return asyncio.run(call())


def test_the_sdk_answers_a_finding_without_evidence_with_the_field_name_alone(
    repo: Path,
) -> None:
    # The failure mode the report_finding PreToolUse hook exists for. The SDK
    # validates the call against the schema before the handler runs, names only
    # the missing property, not what it must hold, and answers an identical
    # retry identically, so a reviewer that keeps leaving it out never learns
    # why and spends turns until the budget runs out.
    collector = FindingCollector(repo)
    args = without("evidence", complete())

    answers = [call_report_tool(repo, collector, args) for _ in range(3)]

    assert answers == [(True, "Input validation error: 'evidence' is a required property")] * 3
    assert collector.findings() == []


def test_a_finding_without_evidence_is_refused_with_what_evidence_must_hold(repo: Path) -> None:
    problem = FindingCollector(repo).problem(without("evidence", complete()))

    assert problem is not None
    assert "missing evidence" in problem
    assert "The code lines relied on, quoted, and the reasoning they support" in problem
    assert "at least 20 characters" in problem
    assert "never leave one out" in problem


def test_every_missing_field_is_named_at_once_and_a_nullable_one_may_be_null(
    repo: Path,
) -> None:
    problem = FindingCollector(repo).problem(without("evidence", RAW))

    assert problem is not None
    assert "evidence (" in problem
    assert "or null when there is none" in problem.split("replacement (", 1)[1]


def test_the_check_says_what_add_would_refuse_and_records_nothing(repo: Path) -> None:
    collector = FindingCollector(repo)

    assert collector.problem(complete()) is None
    assert "confidence" in (collector.problem(complete(confidence=3)) or "")
    assert "has 5 lines" in (collector.problem(complete(line=9, end_line=None)) or "")
    assert collector.findings() == []


def test_the_check_is_as_strict_about_types_as_the_sdk_s_schema(repo: Path) -> None:
    collector = FindingCollector(repo)
    args = complete(line="3", end_line=None)

    is_error, sdk_answer = call_report_tool(repo, collector, args)

    assert is_error and "is not of type 'integer'" in sdk_answer
    assert "line" in (collector.problem(args) or "")
    assert collector.problem(complete(confidence=1)) is None  # an integer is a JSON number
