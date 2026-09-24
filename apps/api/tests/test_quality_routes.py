"""Quality views remain protected, paged and tied to saved formal/shadow hashes."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from wenyi_api import main
from wenyi_api.config_documents import config_document, merge_project, project_document
from wenyi_api.quality_presentation import quality_records
from wenyi_api.routers import review
from wenyi_api.schemas import ReviewRunRequest
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.quality.context import build_context
from wenyi_core.quality.models import digest
from wenyi_core.quality.units import build_quality_units


@pytest.fixture
def quality_store():
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "en", "target": "zh"}}
    )
    chapter = Chapter(
        index=2,
        title="Test",
        segments=[
            Segment(index=4, source="", kind="image"),
            Segment(index=8, source="The gate", target="大门"),
            Segment(index=12, source=" closed.", target="关闭。", cont=True),
            Segment(index=27, source="He waited.", target="他等着。"),
        ],
    )
    units = build_quality_units([chapter])
    artifacts = {
        "reviews/review-test/result.json": {"status": "completed"},
        "reviews/review-test/quality/manifest.json": {"units": [u.identity() for u in units]},
    }
    artifacts[f"reviews/review-test/quality/units/{units[0].unit_id}/decision.json"] = {
        "action": "retranslate",
        "targets": ["PRIVATE CANDIDATE"],
        "score": {"target_parts": ["PRIVATE CANDIDATE"]},
    }
    for i, unit in enumerate(units):
        context = build_context(unit, units, config, [], {})
        artifacts[f"reviews/review-test/quality/units/{unit.unit_id}/baseline.json"] = {
            **unit.identity(),
            "target_parts": list(unit.target_parts),
            "context_hash": context.context_hash,
            "text_scope": "formal",
            "status": "scored",
            "dimensions": {
                "adequacy": {
                    "score": i + 2,
                    "normalized": (i + 2) * 25,
                    "confidence": 0.9,
                    "status": "scored",
                    "probabilities": {"0": 0, "1": 0, "2": 1, "3": 0, "4": 0},
                }
            },
            "request": "PRIVATE PROMPT MUST NOT LEAK",
        }
    terms = []
    storage = SimpleNamespace(
        read_artifact=artifacts.get,
        list_artifacts=lambda prefix: sorted(key for key in artifacts if key.startswith(prefix)),
        load_manifest=lambda: {
            "source_lang": "en",
            "target_lang": "zh",
            "chapters": [{"index": 2}],
        },
        load_chapter=lambda ci: chapter,
        all_terms=lambda: terms,
        load_analysis=lambda: {},
        state_lock=nullcontext,
    )
    return storage, config, artifacts, chapter, units, terms


@pytest.fixture
def quality_api(quality_store, monkeypatch):
    storage, config, *_ = quality_store
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token="test-token"))
    monkeypatch.setattr(
        review,
        "require_project",
        lambda pid: (
            {"id": pid, "fmt": "epub"}
            if pid == "project-a"
            else (_ for _ in ()).throw(review.HTTPException(404, "project not found"))
        ),
    )
    monkeypatch.setattr(review, "storage_for", lambda pid: storage)
    monkeypatch.setattr(review, "effective_config", lambda project: config)
    client = TestClient(main.create_app())
    yield client
    client.close()


def test_quality_pagination_auth_filters_and_safe_summary(quality_api):
    endpoint = "/projects/project-a/review/runs/review-test/quality"
    assert quality_api.get(endpoint).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    response = quality_api.get(endpoint, params={"limit": 1, "max_score": 60}, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 1
    assert result["items"][0]["members"] == [
        {"text_index": 0, "segment_index": 8},
        {"text_index": 1, "segment_index": 12},
    ]
    assert result["items"][0]["stale"] is False
    assert "PRIVATE" not in response.text and "target_parts" not in response.text
    assert (
        quality_api.get(endpoint, params={"offset": 1, "limit": 1}, headers=headers)
        .json()["items"][0]["unit_id"]
        .endswith("seg27")
    )
    assert quality_api.get(endpoint, params={"limit": 201}, headers=headers).status_code == 422
    assert (
        quality_api.get(endpoint.replace("project-a", "project-b"), headers=headers).status_code
        == 404
    )
    assert (
        quality_api.get(endpoint.replace("review-test", "not-a-run"), headers=headers).status_code
        == 404
    )


def test_stale_after_manual_edit_neighbor_change_or_term_change(quality_store):
    storage, config, _, chapter, _, terms = quality_store
    assert not any(row["stale"] for row in quality_records(storage, "review-test", config))
    chapter.segments[2].target = "手动修改。"
    assert all(row["stale"] for row in quality_records(storage, "review-test", config))
    chapter.segments[2].target = "关闭。"
    terms.append(GlossaryTerm(source="gate", target="门扉"))
    assert quality_records(storage, "review-test", config)[0]["stale"]


def test_unpublished_shadow_is_fresh_without_masquerading_as_formal(quality_store):
    storage, config, artifacts, chapter, units, _ = quality_store
    uid = units[0].unit_id
    baseline = artifacts[f"reviews/review-test/quality/units/{uid}/baseline.json"]
    artifacts[f"reviews/review-test/quality/final_scores/shadow/{uid}.json"] = {
        **baseline,
        "target_hash": digest(["门扉", "合上了。"]),
        "formal_base_hash": baseline["target_hash"],
        "formal_base_context_hash": baseline["context_hash"],
    }
    formal = quality_records(storage, "review-test", config)[0]
    shadow = quality_records(storage, "review-test", config, view="shadow")[0]
    assert formal["target_hash"] != shadow["target_hash"]
    assert not formal["stale"] and not shadow["stale"]
    assert shadow["publication_status"] == "not_published"
    chapter.segments[1].target = "用户更新"
    assert quality_records(storage, "review-test", config, view="shadow")[0]["stale"]


def test_details_include_only_requested_unit_candidate_text(quality_api, quality_store):
    _, _, artifacts, _, units, _ = quality_store
    uid = units[0].unit_id
    artifacts[f"reviews/review-test/quality/units/{uid}/candidates/one.json"] = {
        "candidate_id": "one",
        "target_parts": ["门扉", "合上了。"],
        "operation": "review.quality_retranslate",
        "request": "PRIVATE PROMPT",
    }
    response = quality_api.get(
        f"/projects/project-a/review/runs/review-test/quality/{uid}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source_parts"] == ["The gate", " closed."]
    assert result["original_target_parts"] == ["大门", "关闭。"]
    assert result["candidates"][0]["target_parts"] == ["门扉", "合上了。"]
    assert "PRIVATE" not in response.text


def test_run_mode_uses_existing_review_job(monkeypatch):
    jobs = []
    monkeypatch.setattr(review, "require_project", lambda pid: {"fmt": "epub"})
    monkeypatch.setattr(
        review,
        "storage_for",
        lambda pid: SimpleNamespace(
            exists=lambda: True,
            load_manifest=lambda: {"chapters": [{"index": 0}]},
            pending_chapters=lambda: [],
        ),
    )

    async def start(pid, kind, *, params):
        jobs.append((pid, kind, params))
        return {"job_id": "test", "project_id": pid, "kind": kind}

    monkeypatch.setattr(review, "start_job", start)
    asyncio.run(
        review.run_ai_review("project-a", ReviewRunRequest(quality_mode="optimize", autofix=False))
    )
    assert jobs == [("project-a", "review", {"autofix": False, "quality_mode": "optimize"})]


def test_quality_and_judgment_provider_config_roundtrip():
    config = Config.from_dict(
        {
            "llm": {
                "preset": "fake",
                "providers": {"judge": {"kind": "typesafe"}},
                "models": {"jev": {"provider": "judge", "model": "jev-1.13.0"}},
                "routes": {"review.quality_score": {"model": "jev"}},
            },
            "pipeline": {"quality": {"mode": "observe", "max_judge_requests_per_run": 9}},
        }
    )
    document = config_document(config)
    restored = Config.from_dict(yaml.safe_load(yaml.safe_dump(document)))
    assert restored.llm.providers["judge"].kind == "typesafe"
    assert restored.pipeline.quality.mode == "observe"
    selection = project_document(restored)
    assert "providers" not in selection["llm"]
    final = Config.from_dict(merge_project(document, selection))
    assert final.pipeline.quality.max_judge_requests_per_run == 9
    assert final.llm.routes["review.quality_score"].model == "jev"


def test_review_job_freezes_mode_before_worker_client_build(monkeypatch):
    from wenyi_api import job_service

    config = Config.from_dict({"llm": {"preset": "fake"}})
    captured = {}
    monkeypatch.setattr(
        job_service,
        "project_write",
        lambda pid: nullcontext(({"source_path": "uploaded", "status": "done"}, None)),
    )
    monkeypatch.setattr(job_service, "effective_config", lambda project: config)

    def create_job(*args, **kwargs):
        captured.update(kwargs)
        return 1

    async def enqueue(*args, **kwargs):
        return "queued"

    monkeypatch.setattr(job_service.dal, "create_job", create_job)
    monkeypatch.setattr(job_service.dal, "set_project_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(job_service, "enqueue", enqueue)
    asyncio.run(
        job_service.start_job(
            "project-a", "review", params={"quality_mode": "optimize", "autofix": False}
        )
    )
    assert captured["config_snapshot"]["pipeline"]["quality"]["mode"] == "optimize"
    assert captured["config_snapshot"]["pipeline"]["review_autofix"] is False


def test_quality_publication_requires_every_continuation_member(quality_store):
    storage, config, artifacts, _, _, _ = quality_store
    artifacts["reviews/review-test/autofix/index.json"] = {
        "locations": [{"chapter": 2, "index": 0, "status": "applied", "target": "大门"}]
    }
    assert quality_records(storage, "review-test", config)[0]["publication_status"] == "partial"
    artifacts["reviews/review-test/autofix/index.json"]["locations"].append(
        {"chapter": 2, "index": 1, "status": "applied", "target": "关闭。"}
    )
    assert quality_records(storage, "review-test", config)[0]["publication_status"] == "published"


def test_manual_edit_cannot_keep_current_published_label(quality_store):
    storage, config, artifacts, chapter, _, _ = quality_store
    artifacts["reviews/review-test/autofix/index.json"] = {
        "locations": [
            {"chapter": 2, "index": 0, "status": "applied", "target": "大门"},
            {"chapter": 2, "index": 1, "status": "no_net_change", "target": "关闭。"},
        ]
    }
    assert quality_records(storage, "review-test", config)[0]["publication_status"] == "published"
    chapter.segments[1].target = "人工改写"
    row = quality_records(storage, "review-test", config)[0]
    assert row["stale"]
    assert row["publication_status"] == "changed"


def test_unchanged_unit_is_not_reported_as_partially_published(quality_store):
    storage, config, artifacts, _, _, _ = quality_store
    artifacts["reviews/review-test/autofix/index.json"] = {
        "locations": [
            {"chapter": 2, "index": 0, "status": "no_net_change", "target": "大门"},
            {"chapter": 2, "index": 1, "status": "no_net_change", "target": "关闭。"},
        ]
    }
    assert (
        quality_records(storage, "review-test", config)[0]["publication_status"] == "not_published"
    )


def test_generation_quality_route_roundtrip_and_effective_capabilities(monkeypatch):
    from wenyi_api.global_settings import registered_models
    from wenyi_core.llm.operations import configured_operations
    from wenyi_core.llm.router import RoutedLLMClient

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-value")
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: pytest.fail("validation constructed SDK"))
    config = Config.from_dict(
        {
            "llm": {"preset": "deepseek", "routes": {"review.quality_score": {"tier": "cheap"}}},
            "pipeline": {"quality": {"mode": "optimize"}},
        }
    )
    restored = Config.from_dict(yaml.safe_load(yaml.safe_dump(config_document(config))))
    assert restored.llm.routes["review.quality_score"].tier == "cheap"
    assert restored.pipeline.quality.mode == "optimize"
    assert all("judgment" in row["capabilities"] for row in registered_models(restored).values())
    client = RoutedLLMClient(restored.llm)
    client.validate_credentials(configured_operations(restored, "review"))
    assert client.routes["review.quality_compare"].provider_kind == "deepseek"


@pytest.mark.parametrize("source", ["native", "generated"])
def test_quality_api_preserves_judgment_provenance_without_requests(
    quality_api, quality_store, source
):
    _, _, artifacts, _, units, _ = quality_store
    uid = units[0].unit_id
    generated = source == "generated"
    provenance = {
        "source": source,
        "confidence": "self_reported" if generated else "model_distribution",
        "probabilities": "self_reported" if generated else "model_distribution",
        "model_identity": "requested" if generated else "resolved",
    }
    artifacts[f"reviews/review-test/quality/units/{uid}/baseline.json"]["judge"] = {
        "model": "deepseek-flash" if generated else "jev-1.13.0",
        "provenance": provenance,
        "model_identity_kind": "requested" if generated else "resolved",
        "requested_model": "deepseek-flash" if generated else "jev-1.13.0",
        "request": "PRIVATE JUDGE PROMPT",
    }
    response = quality_api.get(
        "/projects/project-a/review/runs/review-test/quality",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200, response.text
    judge = response.json()["items"][0]["judge"]
    assert judge["provenance"] == provenance
    assert judge["model_identity_kind"] == ("requested" if generated else "resolved")
    assert judge["requested_model"] == judge["model"]
    if generated:
        assert judge.get("resolved_model") is None
    assert "PRIVATE" not in response.text
