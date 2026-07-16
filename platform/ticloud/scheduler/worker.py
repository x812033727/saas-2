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
from datetime import datetime, timezone

from ..config import settings
from ..db import get_session
from ..engine import BudgetExceeded, RunContext, get_engine
from ..models import Run, RunStatus
from .queue import claim_next_run, enqueue_due_jobs

log = logging.getLogger(__name__)

# How long after the deadline a cooperative engine gets to exit cleanly.
GRACE_PERIOD_S = 5.0


def execute_run(run_id: str) -> RunStatus:
    """Execute one claimed run to a terminal status. Returns the status."""
    session = get_session()
    try:
        run = session.get(Run, run_id)
        job = run.job
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
        thread.join(timeout=job.timeout_s)
        if thread.is_alive():
            ctx.cancelled.set()
            thread.join(timeout=GRACE_PERIOD_S)
            _finish(session, run, RunStatus.TIMED_OUT, error=f"exceeded timeout of {job.timeout_s}s")
            return RunStatus.TIMED_OUT

        error = outcome.get("error")
        if error is None:
            result = outcome["result"]
            run.result = {"summary": result.summary, **result.data}
            _finish(session, run, RunStatus.SUCCEEDED)
            return RunStatus.SUCCEEDED

        if isinstance(error, BudgetExceeded):
            _finish(session, run, RunStatus.BUDGET_EXCEEDED, error=str(error))
            return RunStatus.BUDGET_EXCEEDED

        detail = "".join(traceback.format_exception(error)).strip()
        _finish(session, run, RunStatus.FAILED, error=detail)
        _maybe_retry(session, run)
        return RunStatus.FAILED
    finally:
        session.close()


def _finish(session, run: Run, status: RunStatus, error: str | None = None) -> None:
    run.status = status
    run.error = error
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    log.info("run %s finished: %s", run.id, status.value)


def _maybe_retry(session, run: Run) -> None:
    """Non-deterministic failure: schedule a fresh attempt with context."""
    job = run.job
    if run.attempt > job.max_retries:
        log.warning("run %s exhausted retries (%d)", run.id, job.max_retries)
        return
    retry = Run(
        job_id=job.id,
        status=RunStatus.QUEUED,
        attempt=run.attempt + 1,
        # Engines can read this to avoid repeating the same mistake.
        result={"retry_of": run.id, "previous_error": (run.error or "")[:2000]},
    )
    session.add(retry)
    session.commit()
    log.info("scheduled retry %s (attempt %d) for run %s", retry.id, retry.attempt, run.id)


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
