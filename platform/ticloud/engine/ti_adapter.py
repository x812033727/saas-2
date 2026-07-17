"""Adapter for the Ti autonomous dev-team engine (github.com/x812033727/Ti).

Ti runs a multi-expert workshop (PM / engineer / senior engineer / QA) that
clarifies requirements, decomposes tasks, debates architecture, implements
with tests and review, and records knowledge (RESEARCH.md / DECISIONS.md /
lessons library). This adapter drives that workshop headlessly from a
scheduled run.

Design: the workshop executes in a **subprocess** using the interpreter
inside the Ti checkout (``<ti_path>/.venv/bin/python`` running
:mod:`ti_runner`), so Ti's dependency tree stays isolated from the platform.
The runner bridges Ti's live event stream into a line-oriented JSON protocol
on stdout (see ti_runner's docstring); this adapter translates those lines
into RunContext calls, which is where the platform's budget guard, live
trace, and cost accounting all apply:

- ``phase`` lines open/close trace steps for workshop stages,
- ``step`` lines record instantaneous steps (critic verdicts feed the
  trajectory scorer, publish steps carry the PR URL),
- ``cost`` lines feed add_cost (BudgetExceeded kills the subprocess),
- ``lesson`` lines persist to the job's lesson store, and the job's
  accumulated lessons + the previous attempt's error are folded into the
  workshop brief (knowledge flywheel, both directions),
- cancellation (worker deadline) sends SIGTERM for a graceful
  ``request_stop`` and escalates to SIGKILL of the whole process group so a
  wound-down workshop can never keep spending unattended.

Job payload keys: ``repo_url`` and ``brief`` (required), plus optional
``publish_repo``, ``workflow``, ``time_budget_s``, ``auto_publish``,
``keep_workspace`` — forwarded to the runner as-is.

Requires TICLOUD_TI_PATH to point at a Ti checkout.
"""

import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from ..config import settings
from .base import RunContext, RunResult

log = logging.getLogger(__name__)

# SIGTERM asks the workshop to wind down; after this many seconds the whole
# process group is killed. Must stay well inside the worker's grace period.
TERMINATE_GRACE_S = 2.0

_PAYLOAD_FORWARD = (
    "repo_url",
    "brief",
    "publish_repo",
    "workflow",
    "time_budget_s",
    "auto_publish",
    "keep_workspace",
)


def _runner_command(ti_path: str) -> list[str]:
    """Interpreter + runner script. Tests monkeypatch this to fake the runner."""
    venv_python = Path(ti_path) / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    runner = Path(__file__).with_name("ti_runner.py")
    return [python, str(runner), "--ti-path", ti_path]


def _terminate(proc: subprocess.Popen) -> None:
    """Graceful stop, then kill the whole group (Ti spawns experts/git/tests)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=5)


class TiEngine:
    name = "ti"

    def run(self, ctx: RunContext) -> RunResult:
        if not settings.ti_path:
            raise RuntimeError(
                "TiEngine needs TICLOUD_TI_PATH pointing at a Ti checkout. "
                "For a credential-free simulation of the same workflow, use "
                "engine='offline'."
            )
        payload = ctx.payload
        for key in ("repo_url", "brief"):
            if not payload.get(key):
                raise RuntimeError(f"ti engine payload needs '{key}'")

        config = {k: payload[k] for k in _PAYLOAD_FORWARD if k in payload}
        config["lessons"] = [f"{l.title} — {l.content}" for l in ctx.get_lessons()]
        if ctx.previous_error:
            config["previous_error"] = ctx.previous_error

        proc = subprocess.Popen(
            _runner_command(settings.ti_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=settings.ti_path,
            start_new_session=True,  # own process group, so _terminate reaps experts too
        )
        stderr_tail: deque[str] = deque(maxlen=40)
        threading.Thread(
            target=self._drain_stderr, args=(proc, stderr_tail), daemon=True
        ).start()

        result_line: dict | None = None
        fatal: str | None = None
        open_step = None
        try:
            try:
                proc.stdin.write(json.dumps(config))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # runner died at startup — the read loop reports why
            for line in self._protocol_lines(proc, ctx):
                kind = line.get("t")
                if kind == "phase":
                    if open_step is not None:
                        ctx.finish_step(open_step, output={})
                    open_step = ctx.record_step(
                        role="team",
                        name=line.get("name") or "phase",
                        kind="phase",
                        input={"detail": line.get("detail", "")} if line.get("detail") else None,
                    )
                elif kind == "step":
                    step = ctx.record_step(
                        role=line.get("role") or "team",
                        name=line.get("name") or "step",
                        kind=line.get("kind") or "phase",
                    )
                    ctx.finish_step(step, output=line.get("output"))
                elif kind == "cost":
                    ctx.add_cost(
                        float(line.get("cost_usd") or 0.0),
                        int(line.get("tokens_in") or 0),
                        int(line.get("tokens_out") or 0),
                    )
                elif kind == "lesson":
                    if line.get("title"):
                        ctx.record_lesson(line["title"], line.get("content", ""))
                elif kind == "result":
                    result_line = line
                elif kind == "fatal":
                    fatal = line.get("error") or "ti runner reported a fatal error"
        finally:
            _terminate(proc)
            if open_step is not None:
                ctx.finish_step(open_step, output={})

        ctx.check_cancelled()
        if fatal:
            raise RuntimeError(f"ti workshop failed: {fatal}")
        if proc.returncode != 0 or result_line is None:
            tail = "\n".join(stderr_tail)[-1500:]
            raise RuntimeError(
                f"ti runner exited with rc={proc.returncode} without a result. "
                f"stderr tail:\n{tail}"
            )
        return RunResult(
            summary=result_line.get("summary") or "Ti workshop completed",
            data=result_line.get("data") or {},
        )

    def _protocol_lines(self, proc: subprocess.Popen, ctx: RunContext):
        """Yield parsed protocol lines; poll cancellation between reads.

        select() on the pipe keeps reads non-blocking so a hung runner can't
        stop us from noticing ctx.cancelled — on cancellation we terminate
        the subprocess and let the reader drain to EOF.
        """
        stream = proc.stdout
        while True:
            if ctx.cancelled.is_set():
                _terminate(proc)
            ready, _, _ = select.select([stream], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            raw = stream.readline()
            if raw == "":  # EOF — subprocess is done writing
                proc.wait(timeout=10)
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                log.warning("ignoring malformed ti runner line: %.200s", raw)
                continue
            if isinstance(line, dict):
                yield line

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen, tail: deque) -> None:
        try:
            for line in proc.stderr:
                tail.append(line.rstrip())
        except ValueError:  # stream closed during shutdown
            pass
