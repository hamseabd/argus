"""Findings are recorded through a tool, validated one at a time, so no agent re-types them."""

import asyncio
import json
from pathlib import Path

import pytest

from argus.agent.findings import MAX_RECORDED, FindingCollector, report_finding_tool
from argus.agent.tools import REPORT_FINDING_TOOL_NAME, argus_tools

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
