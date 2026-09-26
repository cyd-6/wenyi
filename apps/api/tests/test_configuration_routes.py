"""Configuration endpoints expose routing tools and workflow statistics."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from wenyi_api import main
from wenyi_api.routers import configuration
from wenyi_core.llm.usage import UsageSample, UsageTracker, empty_usage


def test_model_comparison_endpoints_are_not_available(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token=None))
    app = main.create_app()
    client = TestClient(app)
    try:
        assert client.post("/projects/test/models/compare", json={}).status_code == 404
        assert client.get("/projects/test/models/comparisons/test").status_code == 404
    finally:
        client.close()
    schema = app.openapi()
    assert "ModelCompareRequest" not in schema["components"]["schemas"]
    assert "ModelMessage" not in schema["components"]["schemas"]
    assert "/projects/{pid}/models/compare" not in schema["paths"]
    assert "/projects/{pid}/models/comparisons/{job_id}" not in schema["paths"]
    assert "/projects/{pid}/models" in schema["paths"]
    assert "/projects/{pid}/models/check" in schema["paths"]


@pytest.mark.parametrize("initialized", [False, True])
def test_project_stats_read_workflow_usage_and_timing(monkeypatch, initialized):
    tracker = UsageTracker()
    tracker.record(
        "fast", UsageSample(prompt_tokens=2, completion_tokens=3, total_tokens=5), "Translator"
    )
    usage = tracker.summary() if initialized else None
    timing = {"runs": [{"id": "translation", "elapsed_seconds": 8}], "total_seconds": 8}

    def read_artifact(key):
        assert key == "timing.json"
        return timing if initialized else None

    storage = SimpleNamespace(load_usage=lambda: usage, read_artifact=read_artifact)
    monkeypatch.setattr(configuration, "require_project", lambda pid: {"id": pid})
    monkeypatch.setattr(configuration, "storage_for", lambda pid: storage)
    expected = {
        "usage": usage or empty_usage(),
        "timing": timing if initialized else {"runs": [], "total_seconds": 0},
    }
    assert configuration.project_stats("test") == expected
    assert configuration.project_stats("test") == expected


def test_choice_capabilities_and_project_judge_validation():
    from wenyi_api.config_documents import config_document, project_document
    from wenyi_api.project_service import effective_config
    from wenyi_core.config import Config

    caps = configuration.capabilities()
    assert caps["provider_capabilities"]["typesafe"] == {"text": False, "choice": True}
    assert (
        next(op for op in caps["operations"] if op["id"] == "translation.judge")["request_kind"]
        == "choice"
    )
    defaults = Config.from_dict({"llm": {"preset": "fake"}})
    project = {"id": "test-project", "source_lang": "en", "target_lang": "zh"}
    with pytest.raises(ValueError, match="explicit.*translation.judge"):
        effective_config(project, document={"pipeline": {"best_of_three": True}}, defaults=defaults)
    config = effective_config(
        project,
        document={
            "pipeline": {"best_of_three": True},
            "llm": {"routes": {"translation.judge": {"model": "default_cheap"}}},
        },
        defaults=defaults,
    )
    assert project_document(config)["pipeline"]["best_of_three"] is True
    snapshot = Config.from_dict(config_document(config))
    assert snapshot.pipeline.best_of_three is True
    assert snapshot.llm.routes["translation.judge"].model == "default_cheap"
