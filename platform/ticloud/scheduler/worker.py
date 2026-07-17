"""Worker: claims queued runs and executes them under agent-native guards.

Guards enforced here rather than inside engines:
- timeout: engine runs in a thread; on deadline we set ctx.cancelled and
  give it a grace period to exit cleanly, then mark the run TIMED_OUT.
- budget: RunContext.add_cost raises BudgetExceeded mid-run.
- retry: a generic failure re-enqueues a fresh attempt (up to
  job.max_retries) carrying the previous error as context — budget and
  timeout failures are deterministic and are NOT retried.
"""

import logging
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import settings
from ..db import get_session
from ..engine import BudgetExceeded, RunContext, get_engine
from ..eval import score_run
from ..eval.notify import raise_alert
from ..models import Run, RunStatus, ScoreRecord
from .queue import claim_next_run, enqueue_due_jobs

log = logging.getLogger(__name__)

# How long after the deadline a cooperative engine gets to exit cleanly.
GRACE_PERIOD_S = 5.0
# How often a running engine is checked for a cross-process cancel request.
CANCEL_POLL_S = 2.0


def _cancel_requested(run_id: str) -> bool:
    """Read Run.cancel_requested on a fresh session so the worker sees the
    API's commit from another connection (avoids a stale read snapshot)."""
    s = get_session()
    try:
        return bool(s.scalar(select(Run.cancel_requested).where(Run.id == run_id)))
    finally:
        s.close()


def execute_run(run_id: str) -> RunStatus:
    """Execute one claimed run to a terminal status. Returns the status."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        job = run.job
        # Pre-execution approval gate: never run the engine on an
        # approval-required job until a human has approved this run. Hold it
        # in AWAITING_APPROVAL and stop — approve requeues it (approved), so
        # it won't re-gate; reject terminates it.
        if job.approval_required and run.approval_state != "approved":
            run.status = RunStatus.AWAITING_APPROVAL
            run.approval_state = "pending"
            session.commit()
            raise_alert(
                session,
                job.id,
                kind="approval_required",
                message=f"job '{job.name}' run is awaiting approval before it runs",
                run_id=run.id,
            )
            log.info("run %s awaiting approval", run.id)
            return RunStatus.AWAITING_APPROVAL
        if run.status != RunStatus.RUNNING:  # direct execution without a claim
            run.status = RunStatus.RUNNING
            run.started_at = datetime.now(timezone.utc)
            session.commit()
        ctx = RunContext(session, run, budget_usd=job.budget_usd)

        outcome: dict = {}

        def _target() -> None:
            try:
                engine = get_engine(job.engine)
                outcome["result"] = engine.run(ctx)
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        # Poll so the deadline AND a cross-process cancel request are both
        # observed (the API sets Run.cancel_requested from another process).
        deadline = time.monotonic() + job.timeout_s
        while thread.is_alive():
            thread.join(timeout=min(CANCEL_POLL_S, max(0.0, deadline - time.monotonic())))
            if not thread.is_alive():
                break
            if time.monotonic() >= deadline:
                ctx.cancelled.set()
                thread.join(timeout=GRACE_PERIOD_S)
                _finish(
                    session, run, RunStatus.TIMED_OUT, error=f"exceeded timeout of {job.timeout_s}s"
                )
                _score_and_gate(session, run)
                return RunStatus.TIMED_OUT
            if _cancel_requested(run_id):
                ctx.cancelled.set()
                thread.join(timeout=GRACE_PERIOD_S)
                # A user cancel is not a quality signal: don't score, alert, or retry.
                _finish(session, run, RunStatus.CANCELLED, error="cancelled by user")
                return RunStatus.CANCELLED

        error = outcome.get("error")
        if error is None:
            result = outcome["result"]
            run.result = {"summary": result.summary, **result.data}
            _finish(session, run, RunStatus.SUCCEEDED)
            _score_and_gate(session, run)
            return RunStatus.SUCCEEDED

        if isinstance(error, BudgetExceeded):
            _finish(session, run, RunStatus.BUDGET_EXCEEDED, error=str(error))
            _score_and_gate(session, run)
            return RunStatus.BUDGET_EXCEEDED

        detail = "".join(traceback.format_exception(error)).strip()
        _finish(session, run, RunStatus.FAILED, error=detail)
        _record_failure_lesson(ctx, run)
        if not _maybe_retry(session, run):
            # Final failure — no retry pending, so this is what a human sees.
            raise_alert(
                session,
                job.id,
                kind="run_failed",
                message=f"job '{job.name}' failed after {run.attempt} attempt(s): "
                + (run.error or "").splitlines()[-1][:300],
                run_id=run.id,
            )
            _score_and_gate(session, run)
        return RunStatus.FAILED
    finally:
        session.close()


def _finish(session, run: Run, status: RunStatus, error: str | None = None) -> None:
    run.status = status
    run.error = error
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    log.info("run %s finished: %s", run.id, status.value)


def _maybe_retry(session, run: Run) -> bool:
    """Non-deterministic failure: schedule a fresh attempt with context."""
    job = run.job
    if run.attempt > job.max_retries:
        log.warning("run %s exhausted retries (%d)", run.id, job.max_retries)
        return False
    # Exponential backoff: attempt N (1-based) waits backoff * 2^(N-1). The
    # claim query only picks runs whose scheduled_at has arrived, so a future
    # scheduled_at genuinely delays the retry.
    delay = job.retry_backoff_s * (2 ** (run.attempt - 1)) if job.retry_backoff_s else 0
    retry = Run(
        job_id=job.id,
        status=RunStatus.QUEUED,
        attempt=run.attempt + 1,
        scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
        # Engines can read this to avoid repeating the same mistake.
        result={"retry_of": run.id, "previous_error": (run.error or "")[:2000]},
    )
    session.add(retry)
    session.commit()
    log.info(
        "scheduled retry %s (attempt %d, +%ds) for run %s",
        retry.id, retry.attempt, delay, run.id,
    )
    return True


def _record_failure_lesson(ctx: RunContext, run: Run) -> None:
    """Every failure becomes a lesson future runs (incl. the retry) consult."""
    from ..eval.failures import error_signature, normalize_error

    try:
        last_step = run.steps[-1].name if run.steps else "before first step"
        ctx.record_lesson(
            title=f"failure:{error_signature(run.error or '')}",
            content=(
                f"Run failed at '{last_step}' (attempt {run.attempt}): "
                f"{normalize_error(run.error or '')}. "
                f"Last error detail: {(run.error or '').splitlines()[-1][:500]}"
            ),
        )
    except Exception:  # noqa: BLE001 - knowledge capture must not break the worker
        log.exception("failed to record lesson for run %s", run.id)


def _score_and_gate(session, run: Run) -> None:
    """Quality gate: score the finished run; alert / pause below threshold.

    This is the unattended-agent safety net — nobody watches each run, so
    the platform does. Scoring failures must never take the worker down.
    """
    job = run.job
    try:
        overall, results = score_run(run, session, job.scorers or {})
    except Exception:  # noqa: BLE001
        log.exception("scoring crashed for run %s", run.id)
        return

    run.score = overall
    for r in results:
        session.add(
            ScoreRecord(run_id=run.id, scorer=r.scorer, score=r.score, passed=r.passed, detail=r.detail)
        )
    session.commit()
    log.info("run %s scored %.3f (%s)", run.id, overall, ", ".join(f"{r.scorer}={r.score:.2f}" for r in results))

    threshold = job.score_threshold
    if threshold is None or overall >= threshold:
        return

    raise_alert(
        session,
        job.id,
        kind="low_score",
        message=f"job '{job.name}' scored {overall:.2f}, below threshold {threshold:.2f}",
        run_id=run.id,
    )
    if job.on_low_score == "pause":
        job.paused = True
        session.commit()
        raise_alert(
            session,
            job.id,
            kind="auto_paused",
            message=f"job '{job.name}' auto-paused by quality gate (score {overall:.2f} < {threshold:.2f})",
            run_id=run.id,
        )
        log.warning("job %s auto-paused by quality gate", job.name)


def worker_loop(stop: threading.Event | None = None) -> None:
    """Main loop: tick the scheduler, then drain the queue."""
    stop = stop or threading.Event()
    last_tick = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_tick >= settings.tick_interval:
            session = get_session()
            try:
                enqueue_due_jobs(session)
            finally:
                session.close()
            last_tick = now

        session = get_session()
        try:
            run = claim_next_run(session)
        finally:
            session.close()

        if run is not None:
            execute_run(run.id)
        else:
            stop.wait(settings.poll_interval)


def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from ..db import init_db

    init_db()
    log.info("ticloud worker started")
    worker_loop()


if __name__ == "__main__":  # pragma: no cover
    main()
