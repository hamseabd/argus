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
