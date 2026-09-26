"""Scorer framework: turn a finished run into a 0..1 quality score.

Rule-based scorers are always on (no LLM, no keys, deterministic); the
LLM judge joins in when configured and credentialed. The overall score is
a weighted mean — except that failing a *required* scorer zeroes the run,
because "the agent didn't finish" can't be averaged away by a nice
trajectory.
"""

import math
from dataclasses import dataclass, field
from numbers import Real
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import Run


@dataclass
class ScoreResult:
    scorer: str
    score: float  # 0..1
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    required: bool = False


# Populated by rules.py / judge.py at import time (name -> callable).
# Signature: (run, session, config) -> ScoreResult | None (None = skipped).
SCORERS: dict[str, Callable[[Run, Session, dict], ScoreResult | None]] = {}


def register(name: str):
    def deco(fn):
        SCORERS[name] = fn
        return fn
    return deco


def score_run(run: Run, session: Session, config: dict | None = None) -> tuple[float, list[ScoreResult]]:
    """Score a finished run with every enabled scorer.

    config comes from Job.scorers, keyed by scorer name:
    {"judge": {"enabled": true}, "cost_anomaly": {"factor": 5}}
    A scorer crashing is recorded as a zero from that scorer, never an
    unscored run — the gate must fail closed, not open.
    """
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return 0.0, [
            ScoreResult(
                scorer="scorers",
                score=0.0,
                passed=False,
                detail={"error": "scorers config must be an object"},
                required=True,
            )
        ]
    unknown_scorers = sorted(set(config) - set(SCORERS))
    if unknown_scorers:
        return 0.0, [
            ScoreResult(
                scorer=name,
                score=0.0,
                passed=False,
                detail={"error": "unknown scorer"},
                required=True,
            )
            for name in unknown_scorers
        ]
    results: list[ScoreResult] = []
    for name, fn in SCORERS.items():
        scorer_cfg = config.get(name, {})
        if not isinstance(scorer_cfg, dict):
            results.append(
                ScoreResult(
                    scorer=name,
                    score=0.0,
                    passed=False,
                    detail={"error": "scorer config must be an object"},
                    required=True,
                )
            )
            continue
        enabled = scorer_cfg.get("enabled", True)
        if not isinstance(enabled, bool):
            results.append(
                ScoreResult(
                    scorer=name,
                    score=0.0,
                    passed=False,
                    detail={"error": "scorer enabled flag must be a boolean"},
                    required=True,
                )
            )
            continue
        if not enabled:
            continue
        try:
            result = fn(run, session, scorer_cfg)
        except Exception as exc:  # noqa: BLE001 - fail closed, keep scoring
            result = ScoreResult(scorer=name, score=0.0, passed=False, detail={"error": str(exc)})
        if result is None:
            continue
        if not isinstance(result, ScoreResult):
            results.append(
                ScoreResult(
                    scorer=name,
                    score=0.0,
                    passed=False,
                    detail={
                        "error": "scorer must return ScoreResult or None",
                        "type": type(result).__name__,
                    },
                    required=True,
                )
            )
        else:
            results.append(result)

    if not results:
        return 1.0, []
    for i, result in enumerate(results):
        if not isinstance(result.passed, bool):
            invalid = ScoreResult(
                scorer=result.scorer,
                score=0.0,
                passed=False,
                detail={
                    "error": "scorer passed flag must be a boolean",
                    "passed": repr(result.passed),
                },
                required=True,
            )
            return 0.0, [*results[:i], invalid, *results[i + 1:]]
        if not isinstance(result.required, bool):
            invalid = ScoreResult(
                scorer=result.scorer,
                score=0.0,
                passed=False,
                detail={
                    "error": "scorer required flag must be a boolean",
                    "required": repr(result.required),
                },
                required=True,
            )
            return 0.0, [*results[:i], invalid, *results[i + 1:]]
        score = result.score
        if (
            isinstance(score, bool)
            or not isinstance(score, Real)
            or not math.isfinite(score)
            or not 0.0 <= score <= 1.0
        ):
            invalid = ScoreResult(
                scorer=result.scorer,
                score=0.0,
                passed=False,
                detail={
                    "error": "scorer score must be a finite number between 0 and 1",
                    "score": repr(score),
                },
                required=True,
            )
            return 0.0, [*results[:i], invalid, *results[i + 1:]]
        weight = result.weight
        if (
            isinstance(weight, bool)
            or not isinstance(weight, Real)
            or not math.isfinite(weight)
            or weight <= 0
        ):
            invalid = ScoreResult(
                scorer=result.scorer,
                score=0.0,
                passed=False,
                detail={
                    "error": "scorer weight must be a positive finite number",
                    "weight": repr(weight),
                },
                required=True,
            )
            return 0.0, [*results[:i], invalid, *results[i + 1:]]
    if any(r.required and not r.passed for r in results):
        return 0.0, results
    total_weight = sum(r.weight for r in results)
    overall = sum(r.score * r.weight for r in results) / total_weight
    return round(overall, 4), results


# Import for side effects: registers the built-in scorers.
from . import rules  # noqa: E402,F401
from . import judge  # noqa: E402,F401
