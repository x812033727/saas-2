"""Rule-based scorers: deterministic, LLM-free, always available.

These catch the failure modes that matter most for unattended agents —
didn't finish, got stuck in a loop, quality reviews rejected the work,
or cost quietly ballooned versus this job's own history.
"""

import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Run, RunStatus, TERMINAL_STATUSES
from .base import ScoreResult, register


@register("completion")
def completion(run: Run, session: Session, cfg: dict) -> ScoreResult:
    """Did the run finish successfully? Required: a dead run scores zero overall."""
    ok = run.status == RunStatus.SUCCEEDED
    return ScoreResult(
        scorer="completion",
        score=1.0 if ok else 0.0,
        passed=ok,
        detail={"status": run.status.value},
        required=True,
    )


@register("trajectory")
def trajectory(run: Run, session: Session, cfg: dict) -> ScoreResult:
    """Trajectory health: no stuck loops, bounded steps, reviews approving.

    Catches the "silent failure" class — output looked fine but the
    process was pathological.
    """
    steps = run.steps
    if not steps:
        return ScoreResult(scorer="trajectory", score=0.0, passed=False, detail={"reason": "no steps recorded"})

    detail: dict = {"steps": len(steps)}
    score = 1.0

    # Stuck loop: the same (role, name) repeated >= loop_threshold times in a row.
    loop_threshold = cfg.get("loop_threshold", 3)
    longest, streak, prev = 1, 1, None
    for s in steps:
        key = (s.role, s.name)
        streak = streak + 1 if key == prev else 1
        longest = max(longest, streak)
        prev = key
    detail["longest_repeat"] = longest
    if longest >= loop_threshold:
        score -= 0.5
        detail["loop_detected"] = True

    # Step budget: unattended runs that balloon in length are drifting.
    max_steps = cfg.get("max_steps", 50)
    if len(steps) > max_steps:
        score -= 0.3
        detail["over_max_steps"] = max_steps

    # Review verdicts: any step whose output carries verdict != approve.
    verdicts = [s.output.get("verdict") for s in steps if s.output and "verdict" in s.output]
    if verdicts:
        approvals = sum(1 for v in verdicts if v == "approve")
        detail["review_approval_rate"] = round(approvals / len(verdicts), 2)
        score -= 0.4 * (1 - approvals / len(verdicts))

    score = max(0.0, round(score, 4))
    return ScoreResult(scorer="trajectory", score=score, passed=score >= 0.5, detail=detail)


@register("cost_anomaly")
def cost_anomaly(run: Run, session: Session, cfg: dict) -> ScoreResult | None:
    """This run's cost versus the job's own history — instant drift signal."""
    history = session.scalars(
        select(Run)
        .where(
            Run.job_id == run.job_id,
            Run.id != run.id,
            Run.status.in_(TERMINAL_STATUSES),
        )
        .order_by(Run.scheduled_at.desc())
        .limit(cfg.get("window", 20))
    ).all()
    baseline_costs = [r.cost_usd for r in history if r.cost_usd > 0]
    if len(baseline_costs) < cfg.get("min_history", 3):
        return None  # not enough history to judge — skip, don't guess

    median = statistics.median(baseline_costs)
    factor = cfg.get("factor", 3.0)
    ratio = run.cost_usd / median if median else 0.0
    # 1.0 at/below the anomaly bar, then linear decay; 0 at 2x the bar.
    score = 1.0 if ratio <= factor else max(0.0, round(1 - (ratio - factor) / factor, 4))
    return ScoreResult(
        scorer="cost_anomaly",
        score=score,
        passed=ratio <= factor,
        detail={"cost_usd": round(run.cost_usd, 4), "median_usd": round(median, 4), "ratio": round(ratio, 2)},
    )
