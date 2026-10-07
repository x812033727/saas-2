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
from datetime import datetime, timezone

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

_TRIAGE_RULES = [
    (
        "timeout",
        ("timeout", "timed out", "deadline exceeded"),
        "Check for a stuck step, then raise timeout_s only if the work is expected to run longer.",
    ),
    (
        "budget",
        ("budget", "cost cap", "spend cap", "quota_exceeded"),
        "Reduce scope or raise the job budget before retrying.",
    ),
    (
        "rate_limit",
        ("rate limit", "rate_limit", "ratelimit", "too many requests", "429"),
        "Back off retries or lower concurrency before retrying.",
    ),
    (
        "auth",
        ("unauthorized", "forbidden", "permission denied", "invalid token", "401", "403"),
        "Refresh credentials and confirm the job has access to the target resource.",
    ),
    (
        "network",
        ("connection", "dns", "network", "ssl", "tls", "connection refused", "connection reset"),
        "Verify the upstream service is reachable from the worker.",
    ),
    (
        "dependency",
        ("modulenotfounderror", "importerror", "no module named"),
        "Install the missing dependency or fix the runtime environment.",
    ),
    (
        "validation",
        ("validationerror", "invalid", "malformed", "jsondecodeerror", "parse error"),
        "Fix the job payload or generated output shape before retrying.",
    ),
]
DEFAULT_FAILURE_CATEGORY = "runtime"
DEFAULT_TRIAGE_HINT = "Inspect the latest run trace and promote repeats into an eval case."
FAILURE_CATEGORIES = tuple(category for category, _, _ in _TRIAGE_RULES) + (
    DEFAULT_FAILURE_CATEGORY,
)


def classify_failure(error: str) -> tuple[str, str]:
    """Return a coarse, action-oriented failure category and triage hint."""
    text = (error or "").lower()
    for category, needles, hint in _TRIAGE_RULES:
        if any(needle in text for needle in needles):
            return category, hint
    return DEFAULT_FAILURE_CATEGORY, DEFAULT_TRIAGE_HINT


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


def semantic_failure_key(error: str, category: str | None = None) -> str:
    """Stable family key for semantic grouping without external embeddings."""
    category = category or classify_failure(error)[0]
    if category != DEFAULT_FAILURE_CATEGORY:
        return category
    summary = normalize_error(error).lower()
    match = re.match(r"([a-z_][a-z0-9_]*(?:error|exception))\b", summary)
    return f"runtime:{match.group(1)}" if match else DEFAULT_FAILURE_CATEGORY


def error_signature(error: str) -> str:
    return hashlib.sha1(normalize_error(error).encode()).hexdigest()[:12]


@dataclass
class FailureMode:
    signature: str
    summary: str  # normalized error text, human-readable
    category: str = DEFAULT_FAILURE_CATEGORY
    triage_hint: str = DEFAULT_TRIAGE_HINT
    semantic_key: str = DEFAULT_FAILURE_CATEGORY
    count: int = 0
    job_ids: set = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sample_run_ids: list = field(default_factory=list)
    latest_run_id: str | None = None


def cluster_failures(
    session: Session,
    job_id: str | None = None,
    limit_runs: int = 500,
    job_ids: list[str] | None = None,
    min_count: int = 1,
) -> list[FailureMode]:
    """Group terminal failed runs by error signature, most frequent first.

    job_ids restricts clustering to those jobs (hosted mode: one tenant's
    jobs), so a signature shared across tenants never leaks foreign runs.
    """
    stmt = (
        select(Run)
        .where(Run.status.in_(FAILURE_STATUSES))
        .order_by(Run.scheduled_at.desc())
        .limit(limit_runs)
    )
    if job_id:
        stmt = stmt.where(Run.job_id == job_id)
    if job_ids is not None:
        stmt = stmt.where(Run.job_id.in_(job_ids))

    modes: dict[str, FailureMode] = {}
    for run in session.scalars(stmt):
        error = run.error or ""
        sig = error_signature(error)
        category, hint = classify_failure(error)
        semantic_key = semantic_failure_key(error, category)
        mode = modes.setdefault(
            sig,
            FailureMode(
                signature=sig,
                summary=normalize_error(error),
                category=category,
                triage_hint=hint,
                semantic_key=semantic_key,
            ),
        )
        mode.count += 1
        mode.job_ids.add(run.job_id)
        ts = run.scheduled_at
        mode.first_seen = ts if mode.first_seen is None or ts < mode.first_seen else mode.first_seen
        if mode.last_seen is None or ts > mode.last_seen:
            mode.last_seen = ts
            mode.latest_run_id = run.id
        if len(mode.sample_run_ids) < 5:
            mode.sample_run_ids.append(run.id)

    return sorted(
        (m for m in modes.values() if m.count >= min_count),
        key=lambda m: (m.count, m.last_seen or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
