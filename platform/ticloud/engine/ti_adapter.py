"""Adapter for the Ti autonomous dev-team engine (github.com/x812033727/Ti).

Ti runs a multi-expert workshop (PM / engineer / senior engineer / QA) that
clarifies requirements, decomposes tasks, debates architecture, implements
with tests and review, and records knowledge (RESEARCH.md / DECISIONS.md /
lessons library). This adapter drives that workshop headlessly from a
scheduled run.

Integration contract (to be wired against Ti's orchestrator in studio/):
  1. Build a workshop config from ctx.payload
     (repo, brief, roles, model preferences, workspace mode).
  2. Start the workshop headlessly (no interactive UI session).
  3. Bridge Ti's stage/role events to ctx.record_step / ctx.finish_step so
     the live trace, cost accounting, and budget guard all apply.
  4. Honor ctx.cancelled between stages (deadline enforcement).
  5. Return a RunResult with the PR/branch URL and workshop verdict.
  6. Bridge Ti's lessons library through ctx.get_lessons() (inject into
     the workshop's context at kickoff) and ctx.record_lesson() (persist
     Ti's retrospective takeaways), so knowledge accumulates per job.

Requires TICLOUD_TI_PATH to point at a Ti checkout.
"""

from ..config import settings
from .base import RunContext, RunResult


class TiEngine:
    name = "ti"

    def run(self, ctx: RunContext) -> RunResult:
        if not settings.ti_path:
            raise RuntimeError(
                "TiEngine needs TICLOUD_TI_PATH pointing at a Ti checkout. "
                "For a credential-free simulation of the same workflow, use "
                "engine='offline'."
            )
        raise NotImplementedError(
            "Ti headless integration lands once the Ti repo is available in "
            "this workspace — see the integration contract in this module's "
            "docstring."
        )
