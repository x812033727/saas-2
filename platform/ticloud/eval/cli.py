"""Eval runner CLI — replay eval cases and gate CI on regressions.

    python -m ticloud.eval.cli run [--job JOB_ID] [--min-score X]
    python -m ticloud.eval.cli list

Each enabled EvalCase is executed against a dedicated `eval:<name>` job
(fresh lesson scope — the case tests raw behavior, not memorized fixes),
scored by the same scorer pipeline as production runs, and compared to
its min_score. Any case below threshold exits 1, which is what blocks a
PR merge when wired into CI (see .github/workflows/eval-gate.yml).
"""

import argparse
import sys

from sqlalchemy import select

from ..db import get_session, init_db
from ..models import EvalCase, Job, Run
from ..scheduler.queue import enqueue_manual
from ..scheduler.worker import execute_run


def _eval_job(session, case: EvalCase) -> Job:
    name = f"eval:{case.name}"
    job = session.scalar(select(Job).where(Job.name == name))
    if job is None:
        job = Job(name=name, engine=case.engine, payload=case.payload, max_retries=0)
        session.add(job)
    else:
        job.engine = case.engine
        job.payload = case.payload
    session.commit()
    return job


def run_cases(job_id: str | None = None, min_score_override: float | None = None) -> int:
    session = get_session()
    try:
        stmt = select(EvalCase).where(EvalCase.enabled.is_(True)).order_by(EvalCase.created_at)
        if job_id:
            stmt = stmt.where(EvalCase.job_id == job_id)
        cases = session.scalars(stmt).all()
        if not cases:
            print("no eval cases to run")
            return 0

        failures = 0
        print(f"{'CASE':40} {'SCORE':>7} {'MIN':>6}  RESULT")
        for case in cases:
            job = _eval_job(session, case)
            run = enqueue_manual(session, job)
            execute_run(run.id)
            session.expire_all()
            run = session.get(Run, run.id)

            minimum = min_score_override if min_score_override is not None else case.min_score
            score = run.score if run.score is not None else 0.0
            ok = score >= minimum
            failures += 0 if ok else 1
            print(f"{case.name[:40]:40} {score:7.2f} {minimum:6.2f}  {'PASS' if ok else 'FAIL'}")
            if not ok and run.error:
                print(f"    {run.error.splitlines()[-1][:100]}")

        print(f"\n{len(cases) - failures}/{len(cases)} passed")
        return 1 if failures else 0
    finally:
        session.close()


def list_cases() -> int:
    session = get_session()
    try:
        cases = session.scalars(select(EvalCase).order_by(EvalCase.created_at)).all()
        for c in cases:
            flag = "" if c.enabled else " (disabled)"
            src = f" [from {c.source_signature}]" if c.source_signature else ""
            print(f"{c.name}: engine={c.engine} min_score={c.min_score}{src}{flag}")
        print(f"{len(cases)} case(s)")
        return 0
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ticloud.eval.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run eval cases; exit 1 on any regression")
    run_p.add_argument("--job", default=None, help="only cases sourced from this job id")
    run_p.add_argument("--min-score", type=float, default=None, help="override every case's threshold")
    sub.add_parser("list", help="list eval cases")

    args = parser.parse_args(argv)
    init_db()
    if args.command == "run":
        return run_cases(job_id=args.job, min_score_override=args.min_score)
    return list_cases()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
