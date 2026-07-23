"""LLM-as-judge scorer: Claude reviews the run's trajectory and output.

Opt-in (config {"judge": {"enabled": true}}) and gracefully skipped when the
anthropic SDK or ANTHROPIC_API_KEY is absent — the rule-based scorers always
provide a baseline score, so self-hosters without credentials lose nothing.

Judge spend is recorded in the ScoreRecord detail, NOT added to the run's
cost: evaluation cost mixed into agent cost would pollute the drift signal.
"""

import json
import logging
import os

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..models import Run
from .base import ScoreResult, register

log = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "claude-opus-4-8"
MAX_STEP_OUTPUT_CHARS = 500


class JudgeVerdict(BaseModel):
    score: float = Field(ge=0, le=1, description="Overall run quality, 0 to 1")
    reasoning: str = Field(description="One-paragraph justification")
    failure_modes: list[str] = Field(
        default_factory=list,
        description="Concrete failure patterns observed (empty if none)",
    )


def _trace_summary(run: Run) -> str:
    lines = [
        f"Run status: {run.status.value}",
        f"Result: {json.dumps(run.result) if run.result else 'none'}",
        f"Error: {run.error or 'none'}",
        "Steps:",
    ]
    for s in run.steps:
        out = json.dumps(s.output)[:MAX_STEP_OUTPUT_CHARS] if s.output else "-"
        lines.append(f"  {s.index}. [{s.role}/{s.kind}] {s.name} -> {out}")
    return "\n".join(lines)


def _parse_message(client, **kwargs):
    parser = getattr(getattr(client, "messages", None), "parse", None)
    if parser is not None:
        return parser(**kwargs)

    beta_messages = getattr(getattr(client, "beta", None), "messages", None)
    beta_parser = getattr(beta_messages, "parse", None)
    if beta_parser is not None:
        return beta_parser(**kwargs)

    raise RuntimeError("anthropic SDK does not provide messages.parse or beta.messages.parse")


@register("judge")
def judge(run: Run, session: Session, cfg: dict) -> ScoreResult | None:
    if not cfg.get("enabled", False):
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.debug("judge scorer skipped: no ANTHROPIC_API_KEY")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("judge scorer skipped: anthropic SDK not installed")
        return None

    client = anthropic.Anthropic()
    model = cfg.get("model", DEFAULT_JUDGE_MODEL)
    response = _parse_message(
        client,
        model=model,
        max_tokens=2048,
        system=(
            "You are a quality judge for unattended, scheduled AI agent runs. "
            "Score the run's TRAJECTORY, not just its final answer: penalize "
            "stuck loops, steps whose outputs contradict the final result, "
            "reviews that rejected work which shipped anyway, and results "
            "that look complete but whose process was broken (silent "
            "failures). A clean process with a clear, verified result scores "
            "high."
        ),
        messages=[{"role": "user", "content": _trace_summary(run)}],
        output_format=JudgeVerdict,
    )
    verdict = response.parsed_output
    if verdict is None:
        return ScoreResult(scorer="judge", score=0.0, passed=False, detail={"error": "unparseable verdict"})

    threshold = cfg.get("pass_threshold", 0.6)
    return ScoreResult(
        scorer="judge",
        score=round(verdict.score, 4),
        passed=verdict.score >= threshold,
        detail={
            "model": model,
            "reasoning": verdict.reasoning,
            "failure_modes": verdict.failure_modes,
            "judge_tokens": {
                "in": response.usage.input_tokens,
                "out": response.usage.output_tokens,
            },
        },
        weight=cfg.get("weight", 2.0),
    )
