"""Produce reproducible, blinded translation-quality evaluation material.

No API request is made unless --allow-paid-api and explicit request caps are supplied.
The default workflow requires --mock, whose artificial judgments test plumbing only.
Sample labels remain in the evaluation key and never enter a model request.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from pathlib import Path
from typing import Any

from wenyi_core.config import Config
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.llm.base import LLMClient
from wenyi_core.llm.judgments import JudgmentRequest, JudgmentResult, JudgmentUsage, ScoreAnswer
from wenyi_core.llm.usage import UsageSample


class SmokeClient(LLMClient):
    """Deterministic keep-original judgments, deliberately not a quality benchmark."""

    def complete(self, messages, *, operation, json_mode=False, max_tokens=None):
        self.usage.record(
            "mock", UsageSample(1, 1, 2), stage=operation, provider="mock", model="mock"
        )
        return json.dumps({"status": "no_confirmed_issue", "constraints": []})

    def evaluate(self, request: JudgmentRequest, *, operation: str) -> JudgmentResult:
        self.usage.record(
            "mock", UsageSample(10, 5, 15), stage=operation, provider="mock", model="mock"
        )
        return JudgmentResult(
            model="mock-not-a-quality-measurement",
            answers={
                key: ScoreAnswer(
                    score=4.0,
                    legend={str(i): str(i) for i in range(5)},
                    probabilities={str(i): float(i == 4) for i in range(5)},
                    confidence=1.0,
                )
                for key in request.questions
            },
            usage=JudgmentUsage(input_tokens=10, output_tokens=5),
        ).validate_for(request)


def read_samples(path: Path) -> list[dict[str, Any]]:
    samples = []
    identifiers = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for key in ("id", "book_id", "source", "target"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError(f"Each sample requires a nonempty {key}")
        if row["id"] in identifiers:
            raise ValueError("Sample IDs must be unique")
        identifiers.add(row["id"])
        samples.append(row)
    if not samples:
        raise ValueError("The JSONL input contains no samples")
    splits: dict[str, set[str]] = {}
    for row in samples:
        splits.setdefault(row["book_id"], set()).add(row.get("split", "evaluation"))
    if any(len(values) != 1 for values in splits.values()):
        raise ValueError("Split evaluation by book; one book cannot occur in multiple splits")
    return samples


def summarize_ratings(path: Path, output: Path) -> dict[str, Any]:
    """Unblind supplied human pairwise ratings without running any model."""
    keys = {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in (output / "key.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    totals: dict[str, Any] = {
        "wins": 0,
        "ties": 0,
        "losses": 0,
        "major_new_errors": 0,
        "misses": 0,
        "false_alarms": 0,
        "rated": 0,
    }
    strata: dict[str, dict[str, int]] = {}
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rating = json.loads(line)
        identifier = rating.get("id")
        if identifier not in keys or identifier in seen:
            raise ValueError("Ratings require unique known sample IDs")
        row = keys[identifier]
        versions = row["versions"]
        pair = rating.get("compared", [])
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or {versions.get(item) for item in pair} != {"baseline", "quality"}
        ):
            raise ValueError("Each rating must compare the blinded baseline and quality versions")
        preferred = rating.get("preferred")
        if preferred not in [*pair, "tie"]:
            raise ValueError("preferred must be a compared label or tie")
        verdict = (
            "ties"
            if preferred == "tie"
            else "wins"
            if versions[preferred] == "quality"
            else "losses"
        )
        totals[verdict] += 1
        totals["rated"] += 1
        seen.add(identifier)
        for metric in ("major_new_errors", "misses", "false_alarms"):
            count = rating.get(metric, 0)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{metric} must be a nonnegative integer")
            totals[metric] += count
        labels = row.get("labels", {})
        for group, value in (
            ("genre", row.get("genre")),
            ("language", row.get("language_direction")),
            ("category", labels.get("category") if isinstance(labels, dict) else None),
            ("selection", "replaced" if row["changed"] else "unchanged"),
        ):
            slot = strata.setdefault(
                f"{group}:{value or 'unknown'}", {"wins": 0, "ties": 0, "losses": 0, "rated": 0}
            )
            slot[verdict] += 1
            slot["rated"] += 1
    summary = {
        **totals,
        "sample_count": len(keys),
        "coverage": len(seen) / len(keys) if keys else 0,
        "strata": strata,
        "evaluation_source": "supplied human ratings",
        "limitations": "Descriptive counts only; no statistical sufficiency or quality gain is claimed. Report evaluator agreement and book-level uncertainty separately.",
    }
    (output / "human-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def evaluate(args) -> dict[str, Any]:
    from wenyi_core.llm.factory import build_client
    from wenyi_core.llm.operations import configured_operations
    from wenyi_core.pipeline.quality_workflow import QualityWorkflow
    from wenyi_core.pipeline.runtime import PipelineRuntime
    from wenyi_core.review.run_store import ReviewRunStore
    from wenyi_core.storage.file import FileStorage

    samples = read_samples(args.input)
    config = (
        Config.load(str(args.config))
        if args.config
        else Config.from_dict(
            {
                "language": {"source": "en", "target": "zh"},
                "llm": {"preset": "fake"},
            }
        )
    )
    config.pipeline.quality.mode = "optimize"
    config.pipeline.quality.max_judge_requests_per_run = args.max_judge_requests
    config.pipeline.quality.max_generation_requests_per_run = args.max_generation_requests
    config.pipeline.quality.audit.sample_rate = 0
    if not args.mock and (not args.config or args.max_judge_requests <= 0):
        raise ValueError("Paid evaluation requires --config and a positive --max-judge-requests")
    client = SmokeClient() if args.mock else build_client(config)
    if not args.mock:
        client.validate_credentials(configured_operations(config, "review"))
    runtime = PipelineRuntime(config, client)
    rng = random.Random(args.seed)
    blind, key = [], []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wenyi-quality-evaluation-") as directory:
        store = FileStorage(directory)
        store.save_manifest({"fmt": "text", "chapters": []})
        debug = ReviewRunStore(directory, storage=store)
        chapters = [
            Chapter(
                index=i, segments=[Segment(index=0, source=row["source"], target=row["target"])]
            )
            for i, row in enumerate(samples)
        ]
        workflow = QualityWorkflow(
            config,
            client,
            store,
            debug.review_id,
            [],
            flush_usage=lambda artifacts=None: runtime.flush_usage(
                store, scope="quality_evaluation", review=debug, artifacts=artifacts
            ),
        )
        patches = workflow.prelude(chapters, {})
        optimized = {(patch["chapter"], patch["index"]): patch["after"] for patch in patches}
        formal_summary = workflow.final_scores(chapters, {}, scope="formal")
        shadow_summary = workflow.final_scores(chapters, optimized, scope="shadow")
        for i, row in enumerate(samples):
            versions = [
                ("baseline", row["target"]),
                ("quality", optimized.get((i, 0), row["target"])),
            ]
            if isinstance(row.get("budget_control"), str):
                versions.append(("budget_control", row["budget_control"]))
            rng.shuffle(versions)
            blind.append(
                {
                    "id": row["id"],
                    "source": row["source"],
                    "context": row.get("evaluation_context", ""),
                    "versions": {chr(65 + n): text for n, (_, text) in enumerate(versions)},
                    "human_rating": {
                        "winner": None,
                        "major_new_errors": [],
                        "misses": [],
                        "false_alarms": [],
                    },
                }
            )
            key.append(
                {
                    "id": row["id"],
                    "book_id": row["book_id"],
                    "split": row.get("split", "evaluation"),
                    "labels": row.get("labels", {}),
                    "genre": row.get("genre"),
                    "language_direction": f"{config.source_lang}->{config.target_lang}",
                    "versions": {chr(65 + n): name for n, (name, _) in enumerate(versions)},
                    "changed": (i, 0) in optimized,
                }
            )
    report = {
        "mock": args.mock,
        "quality_claim": "none; independent human evaluation required",
        "samples": len(samples),
        "books": len({row["book_id"] for row in samples}),
        "replaced": sum(row["changed"] for row in key),
        "budget_control_coverage": sum("budget_control" in row for row in samples),
        "elapsed_seconds": time.monotonic() - started,
        "usage": client.usage_summary(),
        "cost_estimate": None,
        "currency": None,
        "rate_source_date": None,
        "formal": formal_summary,
        "shadow": shadow_summary,
        "human_results": {"wins": None, "ties": None, "losses": None, "major_new_errors": None},
        "limitations": [
            "Mock scores are not translation-quality evidence.",
            "Token estimates do not precisely cap supplier billing.",
            "Review high-score unchanged samples as well as replacements.",
            "Provide budget_control from a separately budget-matched baseline.",
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, rows in (("blind.jsonl", blind), ("key.jsonl", key)):
        (args.output / filename).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    switch = parser.add_mutually_exclusive_group(required=True)
    switch.add_argument("--mock", action="store_true")
    switch.add_argument("--allow-paid-api", action="store_true")
    switch.add_argument(
        "--summarize-ratings",
        action="store_true",
        help="Read human ratings from --input and existing key.jsonl under --output; no model calls",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--max-judge-requests", type=int, default=50)
    parser.add_argument("--max-generation-requests", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.max_judge_requests < 0 or args.max_generation_requests < 0:
        parser.error("Request budgets cannot be negative")
    try:
        if args.summarize_ratings:
            report = summarize_ratings(args.input, args.output)
            print(json.dumps({"rated": report["rated"], "output": str(args.output)}))
            return
        report = evaluate(args)
    except ValueError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {"mock": report["mock"], "samples": report["samples"], "output": str(args.output)}
        )
    )


if __name__ == "__main__":
    main()
