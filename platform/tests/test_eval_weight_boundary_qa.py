from ticloud.eval import base
from ticloud.eval.base import ScoreResult


def test_score_run_fails_closed_for_non_score_result(monkeypatch):
    def bad_return_type(run, session, cfg):
        return {"score": 1.0, "passed": True}

    monkeypatch.setattr(base, "SCORERS", {"bad_return": bad_return_type})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert len(results) == 1
    assert results[0].scorer == "bad_return"
    assert results[0].score == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert results[0].detail == {
        "error": "scorer must return ScoreResult or None",
        "type": "dict",
    }


def test_score_run_fails_closed_for_non_boolean_passed(monkeypatch):
    def bad_passed(run, session, cfg):
        return ScoreResult(
            scorer="bad_passed",
            score=1.0,
            passed="yes",
            required=True,
        )

    monkeypatch.setattr(base, "SCORERS", {"bad_passed": bad_passed})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert results[0].detail["error"] == "scorer passed flag must be a boolean"
    assert results[0].detail["passed"] == "'yes'"


def test_score_run_fails_closed_for_non_boolean_required(monkeypatch):
    def bad_required(run, session, cfg):
        return ScoreResult(
            scorer="bad_required",
            score=1.0,
            passed=True,
            required="yes",
        )

    monkeypatch.setattr(base, "SCORERS", {"bad_required": bad_required})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert results[0].detail["error"] == "scorer required flag must be a boolean"
    assert results[0].detail["required"] == "'yes'"


def test_score_run_fails_closed_for_non_boolean_enabled(monkeypatch):
    def scorer(run, session, cfg):
        return ScoreResult(scorer="completion", score=1.0, passed=True)

    monkeypatch.setattr(base, "SCORERS", {"completion": scorer})

    overall, results = base.score_run(None, None, {"completion": {"enabled": "false"}})

    assert overall == 0.0
    assert results[0].scorer == "completion"
    assert results[0].passed is False
    assert results[0].required is True
    assert results[0].detail["error"] == "scorer enabled flag must be a boolean"


def test_score_run_fails_closed_for_nan_score(monkeypatch):
    def nan_score(run, session, cfg):
        return ScoreResult(
            scorer="nan",
            score=float("nan"),
            passed=True,
        )

    monkeypatch.setattr(base, "SCORERS", {"nan": nan_score})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert len(results) == 1
    assert results[0].scorer == "nan"
    assert results[0].score == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert (
        results[0].detail["error"]
        == "scorer score must be a finite number between 0 and 1"
    )
    assert results[0].detail["score"] == "nan"


def test_score_run_fails_closed_for_out_of_range_score(monkeypatch):
    def overflowing_score(run, session, cfg):
        return ScoreResult(
            scorer="overflow",
            score=1.2,
            passed=True,
        )

    monkeypatch.setattr(base, "SCORERS", {"overflow": overflowing_score})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert results[0].detail["score"] == "1.2"


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
