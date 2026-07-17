"""Engine interface: anything that can execute a scheduled agent run.

The platform stays engine-agnostic — Ti is the flagship implementation,
but any agent (single LLM loop, multi-expert workshop, ...) plugs in by
implementing :class:`AgentEngine` and recording steps through
:class:`RunContext`. The context enforces the agent-native guards
(budget, deadline) so engines don't have to reimplement them.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Lesson, Run, RunStep


class BudgetExceeded(Exception):
    """Raised by RunContext when a run exceeds its cost budget."""


@dataclass
class RunResult:
    """Engine's summary of a finished run (persisted to Run.result)."""

    summary: str
    data: dict[str, Any] = field(default_factory=dict)


class RunContext:
    """Handle an engine uses to report progress and spend within guards.

    - record_step / finish_step persist the structured trace live, so the
      UI can stream progress while the run executes.
    - add_cost enforces the budget: exceeding it raises BudgetExceeded.
    - cancelled is set by the worker on timeout; long-running engines
      should check it between steps and exit cleanly.
    """

    def __init__(self, session: Session, run: Run, budget_usd: float):
        self._session = session
        self._run = run
        self._budget_usd = budget_usd
        self._step_index = 0
        self.cancelled = threading.Event()

    @property
    def payload(self) -> dict[str, Any]:
        return self._run.job.payload or {}

    @property
    def cost_usd(self) -> float:
        return self._run.cost_usd

    @property
    def previous_error(self) -> str | None:
        """Error from the failed attempt this run retries (set by the worker),
        so engines can carry failure context into the next attempt."""
        return (self._run.result or {}).get("previous_error")

    def note_result(self, **data) -> None:
        """Persist partial result data mid-run, so an outcome an engine
        produces before it later fails/times-out/blows budget isn't lost.

        The worker overwrites run.result with the RunResult on success, so
        this only shows through on the non-success terminal paths (which
        never touch run.result). None values are ignored so a later, richer
        note never clobbers an earlier field with a blank.
        """
        merged = dict(self._run.result or {})
        merged.update({k: v for k, v in data.items() if v is not None})
        self._run.result = merged
        self._session.commit()

    def record_step(
        self,
        role: str,
        name: str,
        kind: str = "phase",
        input: dict | None = None,
    ) -> RunStep:
        step = RunStep(
            run_id=self._run.id,
            index=self._step_index,
            role=role,
            kind=kind,
            name=name,
            input=input,
        )
        self._step_index += 1
        self._session.add(step)
        self._session.commit()
        return step

    def finish_step(
        self,
        step: RunStep,
        output: dict | None = None,
        cost_usd: float = 0.0,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        step.finished_at = datetime.now(timezone.utc)
        step.output = output
        step.cost_usd = cost_usd
        step.tokens_in = tokens_in
        step.tokens_out = tokens_out
        self._session.commit()
        if cost_usd or tokens_in or tokens_out:
            self.add_cost(cost_usd, tokens_in, tokens_out)

    def add_cost(self, cost_usd: float, tokens_in: int = 0, tokens_out: int = 0) -> None:
        self._run.cost_usd += cost_usd
        self._run.tokens_in += tokens_in
        self._run.tokens_out += tokens_out
        self._session.commit()
        if self._run.cost_usd > self._budget_usd:
            raise BudgetExceeded(
                f"run cost ${self._run.cost_usd:.4f} exceeds budget ${self._budget_usd:.2f}"
            )

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise TimeoutError("run cancelled (deadline reached)")

    # --- knowledge flywheel -------------------------------------------------

    def get_lessons(self, limit: int = 20) -> list[Lesson]:
        """Lessons this job has accumulated — consult before starting work."""
        return list(
            self._session.scalars(
                select(Lesson)
                .where(Lesson.job_id == self._run.job_id)
                .order_by(Lesson.updated_at.desc())
                .limit(limit)
            )
        )

    def record_lesson(self, title: str, content: str) -> Lesson:
        """Persist a lesson for future runs. Same (job, title) updates in place."""
        existing = self._session.scalar(
            select(Lesson).where(Lesson.job_id == self._run.job_id, Lesson.title == title)
        )
        if existing is not None:
            existing.content = content
            existing.source_run_id = self._run.id
            existing.updated_at = datetime.now(timezone.utc)
            self._session.commit()
            return existing
        lesson = Lesson(
            job_id=self._run.job_id, title=title, content=content, source_run_id=self._run.id
        )
        self._session.add(lesson)
        self._session.commit()
        return lesson


@runtime_checkable
class AgentEngine(Protocol):
    name: str

    def run(self, ctx: RunContext) -> RunResult:  # pragma: no cover - interface
        ...
