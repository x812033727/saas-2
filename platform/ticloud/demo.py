"""Seed a zero-API-key demo: three jobs that showcase the whole platform.

    python -m ticloud.demo

- nightly-patrol   cron job with a flaky trap -> the knowledge flywheel:
                   the first run fails and records a lesson, the retry
                   reads it and succeeds ("lessons applied" on the run).
- dep-upgrade      recurring job that keeps failing -> the quality gate:
                   scores 0, alerts fire, the schedule auto-pauses, and
                   the failure clusters under #/failures for promotion.
- long-audit       slow workshop -> watch the trace grow live in the UI.

Idempotent: jobs that already exist are left untouched. Runs are executed
inline so the dashboard has data the moment it opens — no worker needed
for the seed itself (start one afterwards for the schedules to fire).
"""

import sys

from sqlalchemy import select

from .db import get_session, init_db
from .models import Job
from .scheduler.queue import enqueue_manual
from .scheduler.worker import execute_run

DEMO_JOBS = [
    {
        "name": "nightly-patrol",
        "engine": "offline",
        "cron": "0 2 * * *",
        "max_retries": 1,
        "payload": {"flaky_fail_at": 2},
        "runs": 2,  # fail once, then succeed with the lesson applied
    },
    {
        "name": "dep-upgrade",
        "engine": "offline",
        "interval_seconds": 3600,
        "max_retries": 0,
        "payload": {"fail_at": 4},
        "score_threshold": 0.9,
        "on_low_score": "pause",
        "runs": 3,  # recurring failure -> clusters + gate pauses the job
    },
    {
        "name": "long-audit",
        "engine": "offline",
        "interval_seconds": 7200,
        "payload": {"sleep_s": 1},
        "runs": 1,
    },
]


def seed() -> int:
    init_db()
    session = get_session()
    try:
        created = 0
        for spec in DEMO_JOBS:
            spec = dict(spec)
            runs = spec.pop("runs")
            if session.scalar(select(Job).where(Job.name == spec["name"])):
                print(f"= {spec['name']} already exists, skipping")
                continue
            from .scheduler.cron import compute_next_run

            job = Job(**spec)
            job.next_run_at = compute_next_run(job)
            session.add(job)
            session.commit()
            created += 1

            for _ in range(runs):
                run = enqueue_manual(session, job)
                execute_run(run.id)
                session.expire_all()
                # Drain any retry the failure scheduled (flywheel demo).
                from .scheduler.queue import claim_next_run

                pending = claim_next_run(session)
                while pending is not None:
                    execute_run(pending.id)
                    session.expire_all()
                    pending = claim_next_run(session)
            print(f"+ {spec['name']}: seeded with {len(job.runs)} run(s)")

        print(f"\ndemo ready ({created} job(s) created) — open http://localhost:8000/ui/")
        return 0
    finally:
        session.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(seed())
