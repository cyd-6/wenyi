"""The book-only CLI mode retains configuration defaults and validates its judge."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient

from tests.test_translation_candidates import candidate_config


@pytest.mark.parametrize(
    "initial,flag,expected",
    [
        (False, [], False),
        (True, [], True),
        (False, ["--best-of-three"], True),
        (True, ["--no-best-of-three"], False),
    ],
)
def test_best_of_three_cli_override(tmp_path, monkeypatch, initial, flag, expected):
    config = candidate_config()
    config.pipeline.best_of_three = initial
    seen = []
    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", lambda _: config)
    source = tmp_path / "source.txt"
    source.write_text("Synthetic source", encoding="utf-8")

    class Orchestrator:
        def __init__(self, config):
            seen.append(config.pipeline.best_of_three)
            self.client = FakeClient()

        def run_all(self, *_args, **_kwargs):
            return {
                "report": {"summary": {"chapters_done": 1, "chapters_total": 1, "terms": 0}},
                "output": "result.epub",
                "store": SimpleNamespace(run_dir=str(tmp_path), load_usage=lambda: None),
            }

    monkeypatch.setattr("wenyi_core.pipeline.orchestrator.Orchestrator", Orchestrator)
    result = CliRunner().invoke(app, ["translate", str(source), *flag])
    assert result.exit_code == 0, result.output
    assert seen == [expected]


def test_cli_requires_judge_before_starting_models(tmp_path, monkeypatch):
    config = Config.from_dict({"llm": {"preset": "fake"}})
    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", lambda _: config)
    source = tmp_path / "source.txt"
    source.write_text("Synthetic source", encoding="utf-8")
    result = CliRunner().invoke(app, ["translate", str(source), "--best-of-three"])
    assert result.exit_code == 1
    assert "translation.judge" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("flag", ["--best-of-three", "--no-best-of-three"])
def test_subtitles_reject_book_candidate_flags(flag):
    result = CliRunner().invoke(app, ["translate", "sample.srt", flag])
    assert result.exit_code == 1
    assert "SRT translation does not support" in result.output
