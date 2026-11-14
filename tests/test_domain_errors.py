import pytest

from argus.domain.errors import (
    AgentRunError,
    ArgusError,
    GitError,
    GitHubError,
    ReviewProtocolError,
)


def test_every_error_is_an_argus_error() -> None:
    for cls in (AgentRunError, ReviewProtocolError, GitHubError, GitError):
        assert issubclass(cls, ArgusError)
    assert issubclass(ArgusError, Exception)


def test_agent_run_error_carries_subtype_cost_and_session() -> None:
    err = AgentRunError(subtype="error_max_turns", cost_usd=1.25, session_id="s1")

    assert err.subtype == "error_max_turns"
    assert err.cost_usd == 1.25
    assert err.session_id == "s1"
    assert "error_max_turns" in str(err)
    assert "1.25" in str(err)


def test_agent_run_error_session_may_be_unknown() -> None:
    err = AgentRunError(subtype="error_during_execution", cost_usd=0.0)

    assert err.session_id is None


def test_github_error_carries_status_and_body() -> None:
    err = GitHubError(status=422, body='{"message": "Unprocessable"}')

    assert err.status == 422
    assert "422" in str(err)
    assert "Unprocessable" in str(err)


def test_github_error_truncates_long_bodies_in_message() -> None:
    err = GitHubError(status=500, body="x" * 5000)

    assert len(str(err)) < 600
    assert err.body == "x" * 5000


def test_errors_can_be_raised_and_caught_as_argus_error() -> None:
    with pytest.raises(ArgusError):
        raise ReviewProtocolError("no structured_output in a success result")
    with pytest.raises(ArgusError):
        raise GitError("git diff failed: fatal")
