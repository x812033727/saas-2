"""Headless Ti workshop runner — executed with the Ti checkout's own Python.

This file is part of the ticloud package but is deliberately stdlib-only at
import time: the adapter launches it as a subprocess using the interpreter
inside the Ti checkout (``<ti_path>/.venv/bin/python``), so Ti's heavy
dependencies (claude-agent-sdk, ...) never need to be installed in the
Ti Cloud environment. ``studio`` is imported lazily inside main() after the
Ti checkout has been put on sys.path.

Protocol (line-oriented JSON on stdout; stderr is free-form logging):
  {"t": "phase",  "name": ..., "detail": ...}      # a workshop stage began
  {"t": "step",   "role": ..., "kind": ..., "name": ..., "output": {...}}
  {"t": "cost",   "cost_usd": ..., "tokens_in": ..., "tokens_out": ...}
  {"t": "lesson", "title": ..., "content": ...}
  {"t": "result", "summary": ..., "data": {...}}   # terminal, exit 0 follows
  {"t": "fatal",  "error": ...}                    # terminal, exit 1 follows

Config (single JSON object on stdin):
  repo_url        required — repo the workshop works on (cloned per run)
  brief           required — the requirement handed to the workshop
  publish_repo    owner/repo the session publishes results to (optional)
  workflow        Ti workflow name, e.g. "fast_track" (optional)
  time_budget_s   soft deadline for graceful wind-down (optional)
  auto_publish    default True — the session opens its own PR
  lessons         list of strings folded into the brief (knowledge flywheel)
  previous_error  error text from the failed attempt this run retries
  keep_workspace  default False — keep the per-run clone for post-mortem

SIGTERM requests a graceful stop (StudioSession.request_stop); the adapter
escalates to SIGKILL if the process doesn't exit in time.
"""

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
import uuid

MAX_TEXT = 2000  # trim free-text payload fields so protocol lines stay small


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _owner_repo(repo_url: str) -> str | None:
    """https://github.com/owner/repo(.git) -> "owner/repo"."""
    part = repo_url.rstrip("/")
    if part.endswith(".git"):
        part = part[: -len(".git")]
    pieces = part.split("github.com/")
    if len(pieces) == 2 and pieces[1].count("/") == 1:
        return pieces[1]
    return None


def _clone(repo_url: str, dest: str) -> None:
    clone_url = repo_url
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and repo_url.startswith("https://github.com/"):
        clone_url = repo_url.replace("https://", f"https://x-access-token:{token}@", 1)
    subprocess.run(
        ["git", "clone", clone_url, dest], check=True, timeout=300, capture_output=True
    )
    # Don't leave the token in the workspace config; Ti's publisher has its
    # own credential handling for pushes.
    subprocess.run(["git", "remote", "set-url", "origin", repo_url], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.email", "noreply@anthropic.com"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "Ti Cloud"], cwd=dest, check=True)


def _build_requirement(config: dict) -> str:
    parts = [config["brief"].strip()]
    lessons = [str(l).strip() for l in config.get("lessons") or [] if str(l).strip()]
    if lessons:
        parts.append(
            "過往教訓（排程平台的知識庫，開工前先讀、避免重蹈）：\n"
            + "\n".join(f"- {l}" for l in lessons)
        )
    prev = (config.get("previous_error") or "").strip()
    if prev:
        parts.append(f"上一次排程執行失敗，錯誤如下（本次請避開同一陷阱）：\n{prev}")
    return "\n\n".join(parts)


def _make_broadcast(state: dict):
    """Bridge StudioEvents into protocol lines. Must never raise."""

    async def broadcast(event) -> None:
        try:
            t = event.type.value
            p = event.payload or {}
            if t == "phase_change":
                emit(
                    {
                        "t": "phase",
                        "name": str(p.get("phase") or "")[:200],
                        "detail": str(p.get("detail") or "")[:500],
                    }
                )
            elif t == "token_usage":
                emit(
                    {
                        "t": "cost",
                        "cost_usd": float(p.get("cost_usd") or 0.0),
                        "tokens_in": int(p.get("prompt_tokens") or 0),
                        "tokens_out": int(p.get("completion_tokens") or 0),
                    }
                )
            elif t == "critic_review":
                emit(
                    {
                        "t": "step",
                        "role": "critic",
                        "kind": "review",
                        "name": f"critic:{p.get('gate', '')}"[:200],
                        "output": {
                            "verdict": "approve" if p.get("passed") else "reject",
                            "text": str(p.get("text") or "")[:MAX_TEXT],
                        },
                    }
                )
            elif t == "run_result":
                # No "verdict" key on purpose: iterative test failures are
                # normal TDD churn, not review rejections.
                emit(
                    {
                        "t": "step",
                        "role": "qa",
                        "kind": "test",
                        "name": "test run",
                        "output": {
                            "passed": bool(p.get("passed")),
                            "detail": str(p.get("detail") or "")[:MAX_TEXT],
                        },
                    }
                )
            elif t == "task_status" and p.get("status") == "done":
                emit(
                    {
                        "t": "step",
                        "role": "engineer",
                        "kind": "task",
                        "name": str(p.get("title") or f"task-{p.get('id')}")[:200],
                        "output": {"status": "done"},
                    }
                )
            elif t == "git_commit":
                state["commit"] = p.get("hash")
            elif t == "publish_result":
                state["publish"] = {
                    k: p.get(k)
                    for k in ("ok", "branch", "pr_url", "pr_number", "merged", "repo", "detail")
                }
                emit(
                    {
                        "t": "step",
                        "role": "team",
                        "kind": "publish",
                        "name": "publish results",
                        "output": {
                            "ok": bool(p.get("ok")),
                            "branch": p.get("branch"),
                            "pr_url": p.get("pr_url"),
                            "detail": str(p.get("detail") or "")[:500],
                        },
                    }
                )
            elif t == "ci_result":
                state["ci_state"] = p.get("state")
            elif t == "conclusion":
                state["conclusion"] = p.get("summary")
            elif t == "error":
                state["last_error"] = str(p.get("message") or "")[:MAX_TEXT]
        except Exception:  # noqa: BLE001 - a bad event must not kill the workshop
            log("broadcast bridge error:\n" + traceback.format_exc())

    return broadcast


async def _run(config: dict) -> int:
    from pathlib import Path

    from studio import workflow as workflow_mod
    from studio.orchestrator import StudioSession

    sid = f"tc{uuid.uuid4().hex[:10]}"
    workspace = tempfile.mkdtemp(prefix=f"ticloud-{sid}-")
    log(f"session {sid}: cloning {config['repo_url']} into {workspace}")
    _clone(config["repo_url"], workspace)

    state: dict = {}
    workflow = None
    if config.get("workflow"):
        workflow = workflow_mod.get_workflow(config["workflow"])

    session = StudioSession(
        sid,
        _make_broadcast(state),
        cwd=Path(workspace),
        repo_url=config["repo_url"],
        publish_repo=config.get("publish_repo"),
        base_repo=_owner_repo(config["repo_url"]),
        clarify=False,  # headless: nobody is around to answer
        time_budget_s=config.get("time_budget_s"),
        auto_publish=bool(config.get("auto_publish", True)),
        workflow=workflow,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, session.request_stop)

    requirement = _build_requirement(config)
    result = await session.run(requirement)

    publish = state.get("publish") or {}
    data = {
        "pr_url": publish.get("pr_url"),
        "branch": publish.get("branch"),
        "merged": publish.get("merged"),
        "ci_state": state.get("ci_state"),
        "commit": result.get("commit") or state.get("commit"),
        "shippable": result.get("shippable"),
        "followups": len(result.get("followups") or []),
        "vision": str(result.get("vision") or "")[:500],
    }
    if state.get("conclusion"):
        data["conclusion"] = state["conclusion"]

    if not result.get("completed"):
        reason = result.get("incomplete_reason") or state.get("last_error") or "unknown"
        emit({"t": "fatal", "error": f"workshop did not complete: {reason}"[:MAX_TEXT]})
        return 1

    if not config.get("keep_workspace"):
        shutil.rmtree(workspace, ignore_errors=True)

    summary = "Ti workshop completed"
    if data.get("pr_url"):
        summary += f" — PR {data['pr_url']}"
    elif data.get("commit"):
        summary += f" — commit {data['commit']}"
    emit({"t": "result", "summary": summary, "data": {k: v for k, v in data.items() if v is not None}})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="headless Ti workshop runner")
    parser.add_argument("--ti-path", required=True, help="path to the Ti checkout")
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="verify the Ti integration surface imports, then exit",
    )
    args = parser.parse_args()

    ti_path = os.path.abspath(args.ti_path)
    os.chdir(ti_path)  # studio.config resolves .env and stores relative to cwd
    sys.path.insert(0, ti_path)

    if args.selfcheck:
        import studio.events  # noqa: F401
        import studio.orchestrator  # noqa: F401
        import studio.workflow  # noqa: F401

        emit({"t": "selfcheck", "ok": True, "ti_path": ti_path})
        return 0

    config = json.loads(sys.stdin.read() or "{}")
    for key in ("repo_url", "brief"):
        if not config.get(key):
            emit({"t": "fatal", "error": f"config missing required key: {key}"})
            return 1

    try:
        return asyncio.run(_run(config))
    except Exception:  # noqa: BLE001 - report and exit nonzero
        emit({"t": "fatal", "error": traceback.format_exc()[-MAX_TEXT:]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
