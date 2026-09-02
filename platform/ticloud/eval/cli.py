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
import json
from pathlib import Path
import sys

from sqlalchemy import select

from ..db import get_session, init_db
from ..models import EvalCase
from .runner import eval_cases_payload


def _format_summary(payload: dict) -> str:
    lines = [
        "### Ti Cloud eval gate",
        "",
        f"- Total: {payload['total']}",
        f"- Passed: {payload['passed']}",
        f"- Failed: {payload['failed']}",
        "",
        "| Case | Score | Min | Result |",
        "|---|---:|---:|---|",
    ]
    for case in payload["cases"]:
        result = "PASS" if case["passed"] else "FAIL"
        lines.append(
            f"| {case['name']} | {case['score']:.2f} | {case['min_score']:.2f} | {result} |"
        )
    return "\n".join(lines) + "\n"


def _append_summary(path: str, payload: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(_format_summary(payload))


def run_cases(
    job_id: str | None = None,
    min_score_override: float | None = None,
    json_output: bool = False,
    summary_file: str | None = None,
) -> int:
    session = get_session()
    try:
        stmt = select(EvalCase).where(EvalCase.enabled.is_(True)).order_by(EvalCase.created_at)
        if job_id:
            stmt = stmt.where(EvalCase.job_id == job_id)
        cases = session.scalars(stmt).all()
        if not cases:
            payload = {"total": 0, "passed": 0, "failed": 0, "cases": []}
            if summary_file:
                _append_summary(summary_file, payload)
            if json_output:
                print(json.dumps(payload))
            else:
                print("no eval cases to run")
            return 0

        if not json_output:
            print(f"{'CASE':40} {'SCORE':>7} {'MIN':>6}  RESULT")
        payload = eval_cases_payload(session, cases, min_score_override)
        for case in payload["cases"]:
            if not json_output:
                print(
                    f"{case['name'][:40]:40} {case['score']:7.2f} "
                    f"{case['min_score']:6.2f}  {'PASS' if case['passed'] else 'FAIL'}"
                )
                if not case["passed"] and case["error"]:
                    print(f"    {case['error'].splitlines()[-1][:100]}")
        if summary_file:
            _append_summary(summary_file, payload)
        if json_output:
            print(
                json.dumps(
                    payload,
                    sort_keys=True,
                )
            )
        else:
            print(f"\n{payload['passed']}/{payload['total']} passed")
        return 1 if payload["failed"] else 0
    finally:
        session.close()


def list_cases(json_output: bool = False) -> int:
    session = get_session()
    try:
        cases = session.scalars(select(EvalCase).order_by(EvalCase.created_at)).all()
        if json_output:
            print(
                json.dumps(
                    {
                        "total": len(cases),
                        "cases": [
                            {
                                "id": c.id,
                                "name": c.name,
                                "job_id": c.job_id,
                                "engine": c.engine,
                                "min_score": c.min_score,
                                "source_signature": c.source_signature,
                                "enabled": c.enabled,
                            }
                            for c in cases
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
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
    run_p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    run_p.add_argument("--summary-file", default=None, help="append a Markdown eval summary to this file")
    list_p = sub.add_parser("list", help="list eval cases")
    list_p.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    args = parser.parse_args(argv)
    init_db()
    if args.command == "run":
        return run_cases(
            job_id=args.job,
            min_score_override=args.min_score,
            json_output=args.json,
            summary_file=args.summary_file,
        )
    return list_cases(json_output=args.json)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
