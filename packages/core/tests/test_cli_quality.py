"""Quality overrides are validated before requests and keep standalone Review independent."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config


@pytest.mark.parametrize(
    "arguments, message",
    [
        (
            ["translate", "input.txt", "--no-review", "--quality-mode", "optimize"],
            "Quality requires whole-book review",
        ),
        (
            ["translate", "input.txt", "--chapter", "0", "--quality-mode", "observe"],
            "--chapter cannot run whole-book quality",
        ),
        (
            ["translate", "input.srt", "--quality-mode", "observe"],
            "SRT translation does not support",
        ),
    ],
)
def test_quality_conflicts_fail_before_api_validation(arguments, message):
    config = Config.from_dict({"llm": {"preset": "fake"}})
    with (
        patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=config),
        patch("wenyi_cli.commands.context.CommandContext.validate_api_configuration") as validate,
    ):
        result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 1, result.output
    assert message in result.output.replace("\n", " ")
    validate.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "observe", "optimize"])
def test_standalone_review_quality_override_precedes_validation(mode):
    config = Config.from_dict({"llm": {"preset": "fake"}, "pipeline": {"review": False}})
    captured = []

    def validate(config, workflow):
        captured.append((config.pipeline.quality.mode, config.pipeline.review_autofix, workflow))

    fake = SimpleNamespace(
        client=SimpleNamespace(interrupt_scope=nullcontext),
        run_review=lambda *_args, **_kwargs: {
            "store": SimpleNamespace(run_dir="not-a-real-state-directory"),
            "review_result": {},
            "review_dir": "not-a-real-review-directory",
        },
    )
    with (
        patch("wenyi_cli.commands.context.CommandContext.load_config", return_value=config),
        patch(
            "wenyi_cli.commands.context.CommandContext.validate_api_configuration",
            side_effect=validate,
        ),
        patch("wenyi_cli.commands.validation.os.path.isfile", return_value=True),
        patch("wenyi_core.pipeline.orchestrator.Orchestrator", return_value=fake),
        patch("wenyi_cli.commands.presentation.print_review_summary"),
        patch("wenyi_cli.commands.presentation.print_timing"),
    ):
        result = CliRunner().invoke(
            app, ["review", "input.txt", "--quality-mode", mode, "--no-autofix"]
        )
    assert result.exit_code == 0, result.output
    assert captured == [(mode, False, "review")]
