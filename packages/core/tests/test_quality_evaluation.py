"""Evaluation smoke outputs are blinded and never presented as quality evidence."""

import argparse
import json
import runpy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "evaluate_translation_quality.py"


def test_smoke_outputs_keep_labels_out_of_requests_and_blind_material(tmp_path, monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    client = module["SmokeClient"]
    evaluate = client.evaluate
    requests = []

    def captured(self, request, *, operation):
        requests.append(request.model_dump(mode="json"))
        return evaluate(self, request, operation=operation)

    monkeypatch.setattr(client, "evaluate", captured)
    source = tmp_path / "input.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": "one",
                "book_id": "book",
                "source": "Do not leave.",
                "target": "不要走。",
                "labels": {"secret_label": "holdout_answer"},
                "budget_control": "别走。",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "results"
    result = module["evaluate"](
        argparse.Namespace(
            input=source,
            output=output,
            config=None,
            mock=True,
            max_judge_requests=10,
            max_generation_requests=0,
            seed=42,
        )
    )
    assert result["mock"] is True
    assert result["samples"] == 1
    assert result["replaced"] == 0
    assert result["cost_estimate"] is None
    assert "holdout_answer" not in json.dumps(requests)
    assert "holdout_answer" not in (output / "blind.jsonl").read_text()
    assert "holdout_answer" in (output / "key.jsonl").read_text()
    assert len(json.loads((output / "blind.jsonl").read_text())["versions"]) == 3
    assert result["human_results"]["wins"] is None
    key = json.loads((output / "key.jsonl").read_text())
    names = {name: label for label, name in key["versions"].items()}
    ratings = tmp_path / "ratings.jsonl"
    ratings.write_text(
        json.dumps(
            {
                "id": "one",
                "compared": [names["baseline"], names["quality"]],
                "preferred": names["quality"],
                "major_new_errors": 1,
            }
        ),
        encoding="utf-8",
    )
    summary = module["summarize_ratings"](ratings, output)
    assert summary["wins"] == 1
    assert summary["major_new_errors"] == 1
    assert summary["coverage"] == 1
    assert summary["strata"]["selection:unchanged"]["rated"] == 1


def test_evaluation_rejects_book_leakage_between_splits(tmp_path):
    module = runpy.run_path(str(SCRIPT))
    source = tmp_path / "input.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": split,
                    "book_id": "same-book",
                    "source": "Text",
                    "target": "译文",
                    "split": split,
                }
            )
            for split in ["development", "evaluation"]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Split evaluation by book"):
        module["read_samples"](source)
