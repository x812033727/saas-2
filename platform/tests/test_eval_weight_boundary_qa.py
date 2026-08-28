from ticloud.eval import base
from ticloud.eval.base import ScoreResult


def test_score_run_fails_closed_for_zero_weight_from_config(monkeypatch):
    def configurable_weight(run, session, cfg):
        return ScoreResult(
            scorer="weighted",
            score=1.0,
            passed=True,
            weight=cfg.get("weight", 1.0),
        )

    monkeypatch.setattr(base, "SCORERS", {"weighted": configurable_weight})

    overall, results = base.score_run(None, None, {"weighted": {"weight": 0}})

    assert overall == 0.0
    assert len(results) == 1
    assert results[0].scorer == "weighted"
    assert results[0].passed is False
    assert results[0].required is True
    assert (
        results[0].detail["error"]
        == "scorer weight must be a positive finite number"
    )
    assert results[0].detail["weight"] == "0"


def test_score_run_fails_closed_for_non_numeric_weight(monkeypatch):
    def bad_weight(run, session, cfg):
        return ScoreResult(
            scorer="weighted",
            score=1.0,
            passed=True,
            weight="heavy",
        )

    monkeypatch.setattr(base, "SCORERS", {"weighted": bad_weight})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert results[0].detail["weight"] == "'heavy'"


def test_score_run_keeps_weighted_average_for_valid_weights(monkeypatch):
    def low(run, session, cfg):
        return ScoreResult(scorer="low", score=0.25, passed=True, weight=2.0)

    def high(run, session, cfg):
        return ScoreResult(scorer="high", score=1.0, passed=True, weight=1.0)

    monkeypatch.setattr(base, "SCORERS", {"low": low, "high": high})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.5
    assert [r.scorer for r in results] == ["low", "high"]
