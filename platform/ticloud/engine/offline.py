"""Offline demo engine — a faithful, zero-API-key simulation of a Ti workshop.

Mirrors Ti's offline demo mode: walks the full multi-expert workflow
(PM clarification → task decomposition → architecture debate → iterative
implementation with QA review → retrospective) with deterministic outputs
and small synthetic costs, so the whole platform — scheduling, tracing,
budget/timeout guards, and the UI — can be exercised end-to-end without
credentials. Also the workhorse for tests.
"""

from ..engine.base import RunContext, RunResult

# (role, step name, synthetic cost in USD, synthetic output)
_WORKFLOW = [
    ("pm", "Clarify requirements", 0.002, {"questions": 3, "assumptions": 2}),
    ("pm", "Decompose into tasks", 0.003, {"tasks": ["task-1", "task-2"]}),
    ("engineer", "Architecture debate", 0.004, {"decision": "layered, DB-queue based"}),
    ("engineer", "Implement task-1", 0.005, {"files_changed": 3, "tests_passed": True}),
    ("qa", "Review task-1", 0.003, {"verdict": "approve", "issues": 0}),
    ("engineer", "Implement task-2", 0.005, {"files_changed": 2, "tests_passed": True}),
    ("qa", "Review task-2", 0.003, {"verdict": "approve", "issues": 0}),
    ("team", "Demo & retrospective", 0.002, {"lessons_recorded": 1}),
]


class OfflineEngine:
    name = "offline"

    def run(self, ctx: RunContext) -> RunResult:
        # Payload knobs used by tests and demos:
        #   steps: truncate the workflow
        #   cost_multiplier: inflate costs to exercise the budget guard
        #   fail_at: step index that raises, to exercise retry
        #   sleep_s: per-step delay, to exercise the timeout guard
        #   flaky_fail_at: fails at step N ONLY when the job has no lesson
        #     about it yet — demonstrates the knowledge flywheel: first run
        #     fails and records a lesson, the next run reads it and avoids
        #     the trap (mirroring how Ti consults its lessons library).
        payload = ctx.payload
        steps = _WORKFLOW[: payload.get("steps", len(_WORKFLOW))]
        multiplier = payload.get("cost_multiplier", 1.0)
        fail_at = payload.get("fail_at")
        sleep_s = payload.get("sleep_s", 0)

        flaky_at = payload.get("flaky_fail_at")
        lessons_applied: list[str] = []
        if flaky_at is not None:
            lessons = ctx.get_lessons()
            if any(l.title.startswith("failure:") for l in lessons):
                lessons_applied = [l.title for l in lessons]
                flaky_at = None  # learned from the previous failure

        for i, (role, name, cost, output) in enumerate(steps):
            if flaky_at == i:
                raise RuntimeError(f"flaky trap at step {i}: {name} (no lesson recorded yet)")
            ctx.check_cancelled()
            step = ctx.record_step(role=role, name=name, kind="phase")
            if sleep_s:
                # Event.wait sleeps but wakes immediately on cancellation.
                ctx.cancelled.wait(timeout=sleep_s)
                ctx.check_cancelled()
            if fail_at == i:
                raise RuntimeError(f"simulated failure at step {i}: {name}")
            ctx.finish_step(
                step,
                output=output,
                cost_usd=cost * multiplier,
                tokens_in=int(400 * multiplier),
                tokens_out=int(150 * multiplier),
            )

        data = {"steps": len(steps), "cost_usd": round(ctx.cost_usd, 4)}
        if lessons_applied:
            data["lessons_applied"] = lessons_applied
        return RunResult(summary="workshop completed (offline demo)", data=data)
