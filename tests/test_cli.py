from typer.testing import CliRunner

from argus import __version__
from argus.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"argus {__version__}"


def test_no_args_shows_help_and_fails() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code != 0
    assert "version" in result.output


def test_a_failure_message_is_redacted_before_it_is_printed_or_logged(capsys) -> None:
    import pytest
    import typer

    from argus import cli

    with pytest.raises(typer.Exit):
        cli._fail("GitHub said: token ghs_" + "b" * 36 + " is bad")

    assert "ghs_bbbb" not in capsys.readouterr().err
