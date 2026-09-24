import pytest
from pydantic import ValidationError
from wenyi_core.config import _DEFAULT_CONFIG_YAML, Config
from wenyi_core.quality.models import DIMENSIONS, QualityConfig
from wenyi_core.quality.policy import priority, route_score
from wenyi_core.quality.selection import accept_candidate


def row(score=90, confidence=0.9):
    return {
        "status": "scored",
        "chapter_index": 0,
        "members": [{"text_index": 0}],
        "dimensions": {
            name: {
                "status": "scored",
                "normalized": score,
                "confidence": confidence,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 0.4, "4": 0.6},
            }
            for name in DIMENSIONS
        },
    }


@pytest.mark.parametrize("mode", ["off", "observe", "optimize"])
def test_quality_modes(mode):
    assert QualityConfig(mode=mode).mode == mode


@pytest.mark.parametrize(
    "settings",
    [
        {"max_candidates_per_unit": 3},
        {"max_judge_requests_per_run": -1},
        {"thresholds": {"adequacy_min": 101}},
        {"thresholds": {"voice_min": float("nan")}},
        {"comparison": {"keep_original_on_tie": False}},
        {"mode": False},
        {"auto_apply": True},
    ],
)
def test_config_rejects_unsafe_or_unknown_fields(settings):
    with pytest.raises(ValidationError):
        QualityConfig(**settings)


def test_real_yaml_default_off_never_becomes_boolean(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_DEFAULT_CONFIG_YAML)
    assert Config.load(str(path)).pipeline.quality.mode == "off"


def test_missing_quality_route_rejected_only_when_enabled():
    assert Config.from_dict({}).pipeline.quality.mode == "off"
    with pytest.raises(ValueError, match="quality_score"):
        Config.from_dict({"pipeline": {"quality": {"mode": "observe"}}})


def test_uncertainty_precedes_critical_risk_and_fluency_cannot_offset_risk():
    score = row()
    score["dimensions"]["adequacy"]["normalized"] = 20
    score["dimensions"]["adequacy"]["confidence"] = 0.2
    assert route_score(score, QualityConfig())["action"] == "needs_review"
    score["dimensions"]["adequacy"]["confidence"] = 0.95
    assert route_score(score, QualityConfig())["action"] == "retranslate"
    score["dimensions"]["adequacy"]["normalized"] = 95
    score["dimensions"]["voice"]["normalized"] = 50
    assert route_score(score, QualityConfig())["action"] == "revise"


def test_serious_tail_risk_not_hidden_by_average_score():
    score = row()
    score["dimensions"]["adequacy"]["probabilities"] = {
        "0": 0,
        "1": 0.25,
        "2": 0,
        "3": 0,
        "4": 0.75,
    }
    assert route_score(score, QualityConfig())["action"] == "retranslate"
    other = row()
    assert priority(score) < priority(other)


def comparisons(choice="candidate"):
    return [
        {
            "candidate_label": label,
            "choice": f"{label}_better" if choice == "candidate" else choice,
            "confidence": 0.95,
            "probabilities": {
                "a_better": 0.95 if label == "a" else 0.02,
                "b_better": 0.95 if label == "b" else 0.02,
                "equivalent": 0.02,
                "insufficient_evidence": 0.01,
            },
        }
        for label in ["a", "b"]
    ]


@pytest.mark.parametrize("verdict", ["equivalent", "insufficient_evidence"])
def test_tie_or_unknown_preserves_original(verdict):
    assert not accept_candidate(row(70), row(90), comparisons(verdict), QualityConfig())[0]


def test_score_micro_gain_and_critical_regression_rejected():
    assert accept_candidate(row(90), row(91), comparisons(), QualityConfig())[1] == [
        "no_confirmed_improvement"
    ]
    new = row(99)
    new["dimensions"]["adequacy"]["normalized"] = 89
    assert accept_candidate(row(90), new, comparisons(), QualityConfig())[1] == [
        "critical_regression"
    ]


def test_swapped_labels_map_to_same_candidate():
    assert accept_candidate(row(70), row(90), comparisons(), QualityConfig())[0]
    biased = comparisons()
    biased[1]["choice"] = "a_better"
    assert accept_candidate(row(70), row(90), biased, QualityConfig())[1] == ["order_conflict"]
