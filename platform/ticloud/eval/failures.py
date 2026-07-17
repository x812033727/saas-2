"""Failure clustering: group failed runs into recurring failure modes.

Signature-based and fully deterministic — error text is normalized (ids,
numbers, paths, line numbers stripped) so the same class of failure lands
in the same bucket regardless of per-run noise. No embedding API needed,
which keeps self-hosting zero-dependency; semantic (embedding) clustering
is a cloud-tier upgrade on top of the same interface.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Run, RunStatus

FAILURE_STATUSES = (RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.BUDGET_EXCEEDED)

_NORMALIZERS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<id>"),
    (re.compile(r'(File ")[^"]+(")'), r"\1<path>\2"),
    (re.compile(r"line \d+"), "line <n>"),
    (re.compile(r"\d+(\.\d+)?"), "<n>"),
    (re.compile(r"[ \t]+"), " "),
]


def normalize_error(error: str) -> str:
    """Collapse per-run noise so equivalent failures compare equal.

    Uses the last line (the actual exception) plus the exception type
    line count as the identity — full tracebacks vary too much.
    """
    lines = [l.strip() for l in (error or "").strip().splitlines() if l.strip()]
    text = lines[-1] if lines else ""
    for pattern, repl in _NORMALIZERS:
        text = pattern.sub(repl, text)
    return text.strip()


def error_signature(error: str) -> str:
    return hashlib.sha1(normalize_error(error).encode()).hexdigest()[:12]


@dataclass
class FailureMode:
    signature: str
    summary: str  # normalized error text, human-readable
    count: int = 0
    job_ids: set = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sample_run_ids: list = field(default_factory=list)
    latest_run_id: str | None = None


def cluster_failures(session: Session, job_id: str | None = None, limit_runs: int = 500) -> list[FailureMode]:
    """Group terminal failed runs by error signature, most frequent first."""
    stmt = (
        select(Run)
        .where(Run.status.in_(FAILURE_STATUSES))
        .order_by(Run.scheduled_at.desc())
        .limit(limit_runs)
    )
    if job_id:
        stmt = stmt.where(Run.job_id == job_id)

    modes: dict[str, FailureMode] = {}
    for run in session.scalars(stmt):
        sig = error_signature(run.error or "")
        mode = modes.setdefault(sig, FailureMode(signature=sig, summary=normalize_error(run.error or "")))
        mode.count += 1
        mode.job_ids.add(run.job_id)
        ts = run.scheduled_at
        mode.first_seen = ts if mode.first_seen is None or ts < mode.first_seen else mode.first_seen
        if mode.last_seen is None or ts > mode.last_seen:
            mode.last_seen = ts
            mode.latest_run_id = run.id
        if len(mode.sample_run_ids) < 5:
            mode.sample_run_ids.append(run.id)

    return sorted(modes.values(), key=lambda m: m.count, reverse=True)
