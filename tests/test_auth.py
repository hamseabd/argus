from argus.auth import CREDENTIAL_ENV_VARS, credential_source


def test_oauth_token_wins_over_api_key() -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "key"}

    assert credential_source(env) == "CLAUDE_CODE_OAUTH_TOKEN"


def test_api_key_alone_is_detected() -> None:
    assert credential_source({"ANTHROPIC_API_KEY": "key"}) == "ANTHROPIC_API_KEY"


def test_no_credentials_returns_none() -> None:
    assert credential_source({}) is None


def test_blank_values_do_not_count() -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "   ", "ANTHROPIC_API_KEY": ""}

    assert credential_source(env) is None


def test_default_env_is_process_environment(monkeypatch) -> None:
    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    assert credential_source() == "ANTHROPIC_API_KEY"
