"""Flagship job templates — the acquisition channel from PLAN.md.

Each template is a named preset (engine, schedule, guards, gate, and a
payload skeleton) so a user starts a nightly repo patrol / dependency
upgrade / CI babysitter with one call instead of hand-writing raw JSON.
``required_payload`` names payload keys the caller must fill (e.g. a repo
URL); the endpoint rejects a blank one so a template can't create a job
that would immediately no-op.

The three flagship templates target the real ``ti`` engine (they open PRs
against your repo); ``demo-workshop`` uses the offline engine so the
catalog is exercisable with zero setup.
"""

TEMPLATES: list[dict] = [
    {
        "id": "nightly-repo-patrol",
        "name": "Nightly repo patrol",
        "description": (
            "Every night, scan a repo for one worthwhile bug or improvement, "
            "fix it with tests, and open a PR for human review."
        ),
        "engine": "ti",
        "cron": "0 3 * * *",
        "timeout_s": 5400,
        "budget_usd": 5.0,
        "max_retries": 1,
        "score_threshold": 0.6,
        "on_low_score": "alert",
        "payload": {
            "repo_url": "",
            "publish_repo": "",
            "brief": (
                "Patrol this repository: find one concrete, self-contained bug "
                "or improvement, implement the fix with tests, and open a PR. "
                "One change at a time; explain the motivation in the PR."
            ),
        },
        "required_payload": ["repo_url"],
    },
    {
        "id": "dependency-upgrade",
        "name": "Dependency & CVE maintenance",
        "description": (
            "Weekly: bump outdated dependencies, patch known CVEs, run the "
            "test suite, and open a PR only if it stays green."
        ),
        "engine": "ti",
        "cron": "0 4 * * 1",
        "timeout_s": 5400,
        "budget_usd": 5.0,
        "max_retries": 1,
        "score_threshold": 0.6,
        "on_low_score": "alert",
        "payload": {
            "repo_url": "",
            "publish_repo": "",
            "brief": (
                "Review dependencies for outdated or vulnerable versions. "
                "Upgrade a small, safe set, run the tests, and open a PR only "
                "if everything stays green. Note any breaking changes."
            ),
        },
        "required_payload": ["repo_url"],
    },
    {
        "id": "ci-babysitter",
        "name": "CI babysitter",
        "description": (
            "On a short interval, look for a red CI run, diagnose the failure, "
            "propose a fix, and open a PR."
        ),
        "engine": "ti",
        "interval_seconds": 3600,
        "timeout_s": 3600,
        "budget_usd": 4.0,
        "max_retries": 1,
        "score_threshold": 0.6,
        "on_low_score": "alert",
        "payload": {
            "repo_url": "",
            "publish_repo": "",
            "brief": (
                "Check for a failing CI run on the default branch. If one is "
                "failing, diagnose the root cause, implement a fix, verify it, "
                "and open a PR describing what broke and why the fix works."
            ),
        },
        "required_payload": ["repo_url"],
    },
    {
        "id": "demo-workshop",
        "name": "Demo workshop (offline, no setup)",
        "description": (
            "A simulated multi-expert workshop on the offline engine — runs "
            "with zero credentials, for trying the platform end to end."
        ),
        "engine": "offline",
        "cron": "0 2 * * *",
        "budget_usd": 2.0,
        "score_threshold": 0.8,
        "on_low_score": "pause",
        "payload": {},
        "required_payload": [],
    },
]

_BY_ID = {t["id"]: t for t in TEMPLATES}

# Job fields a template may set (everything else on a Job keeps its default).
_JOB_FIELDS = (
    "engine",
    "cron",
    "interval_seconds",
    "timeout_s",
    "budget_usd",
    "max_retries",
    "score_threshold",
    "on_low_score",
)


def get_template(template_id: str) -> dict | None:
    return _BY_ID.get(template_id)


def build_job_fields(template: dict, name: str, cron: str | None, payload_overrides: dict) -> dict:
    """Merge a template with caller overrides into JobCreate kwargs.

    payload_overrides are merged over the template's payload skeleton; an
    explicit cron replaces the template's schedule (clearing interval)."""
    fields: dict = {"name": name}
    for key in _JOB_FIELDS:
        if key in template:
            fields[key] = template[key]
    fields["payload"] = {**template.get("payload", {}), **payload_overrides}
    if cron is not None:
        fields["cron"] = cron
        fields["interval_seconds"] = None
    return fields


def missing_required(template: dict, payload: dict) -> list[str]:
    """Required payload keys that are absent or blank after merging."""
    return [k for k in template.get("required_payload", []) if not payload.get(k)]
