from ticloud.eval import base
from ticloud.eval.base import ScoreResult


def test_score_run_fails_closed_after_preserving_prior_valid_result(monkeypatch):
    def good(run, session, cfg):
        return ScoreResult(scorer="good", score=0.9, passed=True)

    def bad(run, session, cfg):
        return {"score": 1.0, "passed": True}

    monkeypatch.setattr(base, "SCORERS", {"good": good, "bad": bad})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert [result.scorer for result in results] == ["good", "bad"]
    assert results[0].score == 0.9
    assert results[0].passed is True
    assert results[1].score == 0.0
    assert results[1].passed is False
    assert results[1].required is True
    assert results[1].detail == {
        "error": "scorer must return ScoreResult or None",
        "type": "dict",
    }


def test_score_run_accepts_exact_zero_and_one_score_boundaries(monkeypatch):
    def zero(run, session, cfg):
        return ScoreResult(scorer="zero", score=0.0, passed=True)

    def one(run, session, cfg):
        return ScoreResult(scorer="one", score=1.0, passed=True)

    monkeypatch.setattr(base, "SCORERS", {"zero": zero, "one": one})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.5
    assert [result.score for result in results] == [0.0, 1.0]
    assert all(result.passed is True for result in results)
    assert all("error" not in result.detail for result in results)


def test_score_run_rejects_bool_score_even_though_bool_is_numeric(monkeypatch):
    def bool_score(run, session, cfg):
        return ScoreResult(scorer="bool_score", score=True, passed=True)

    monkeypatch.setattr(base, "SCORERS", {"bool_score": bool_score})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert results[0].score == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert (
        results[0].detail["error"]
        == "scorer score must be a finite number between 0 and 1"
    )
    assert results[0].detail["score"] == "True"


def test_score_run_rejects_bool_weight_even_though_bool_is_numeric(monkeypatch):
    def bool_weight(run, session, cfg):
        return ScoreResult(
            scorer="bool_weight",
            score=0.75,
            passed=True,
            weight=True,
        )

    monkeypatch.setattr(base, "SCORERS", {"bool_weight": bool_weight})

    overall, results = base.score_run(None, None, {})

    assert overall == 0.0
    assert results[0].score == 0.0
    assert results[0].passed is False
    assert results[0].required is True
    assert (
        results[0].detail["error"]
        == "scorer weight must be a positive finite number"
    )
    assert results[0].detail["weight"] == "True"
