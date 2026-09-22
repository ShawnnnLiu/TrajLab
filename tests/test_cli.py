from typer.testing import CliRunner

from trajlab.cli import app

COMMANDS = {"run", "watch", "postprocess", "manifest", "validate"}


def test_help_lists_all_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in COMMANDS:
        assert name in result.output
