import re
import tomllib
from pathlib import Path

from sqlalchemy import select

from ticloud.eval.runner import _eval_job
from ticloud.models import EvalCase, Job, Tenant


ROOT = Path(__file__).resolve().parents[2]


def _workflow_steps(path: Path) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()
        if line.startswith("      - name: "):
            current = {"name": line.split(": ", 1)[1]}
            steps.append(current)
        elif current is not None and line.startswith("        run: "):
            current["run"] = line.split("run: ", 1)[1]
    return steps


def test_qa_ci_install_step_covers_billing_test_dependencies():
    pyproject = tomllib.loads((ROOT / "platform/pyproject.toml").read_text())
    project_extras = pyproject["project"]["optional-dependencies"]

    assert "billing" in project_extras
    assert any(req.startswith("stripe") for req in project_extras["billing"])

    install_step = next(
        step
        for step in _workflow_steps(ROOT / ".github/workflows/ci.yml")
        if step["name"] == "Install"
    )
    match = re.fullmatch(r'pip install -e "platform\[(?P<extras>[a-z,]+)\]"', install_step["run"])

    assert match, install_step["run"]
    assert {"dev", "billing"} <= set(match.group("extras").split(","))


def test_qa_eval_job_reuse_is_same_tenant_only(session):
    tenant_a = Tenant(id="tenantaaaaaaaaaaaaaaaaaaaaaaaaaa", name="tenant-a")
    tenant_b = Tenant(id="tenantbbbbbbbbbbbbbbbbbbbbbbbbbb", name="tenant-b")
    source_a = Job(
        id="feedface" + ("a" * 24),
        name="source-a",
        tenant_id=tenant_a.id,
        engine="offline",
        payload={"tenant": "a"},
    )
    source_b = Job(
        id="feedface" + ("b" * 24),
        name="source-b",
        tenant_id=tenant_b.id,
        engine="offline",
        payload={"tenant": "b"},
    )
    session.add_all([tenant_a, tenant_b, source_a, source_b])
    session.commit()

    case_a_v1 = EvalCase(
        name="collision",
        job_id=source_a.id,
        engine="offline",
        payload={"version": 1},
    )
    case_a_v2 = EvalCase(
        name="collision",
        job_id=source_a.id,
        engine="offline",
        payload={"version": 2},
    )
    case_b = EvalCase(
        name="collision",
        job_id=source_b.id,
        engine="offline",
        payload={"version": "b"},
    )

    eval_a_first = _eval_job(session, case_a_v1)
    eval_a_again = _eval_job(session, case_a_v2)
    eval_b = _eval_job(session, case_b)

    assert eval_a_first.id == eval_a_again.id
    assert eval_a_again.payload == {"version": 2}
    assert eval_b.id != eval_a_again.id
    assert eval_b.name == eval_a_again.name == "eval:feedface:collision"
    assert {eval_a_again.tenant_id, eval_b.tenant_id} == {tenant_a.id, tenant_b.id}

    eval_jobs = session.scalars(
        select(Job).where(Job.name == "eval:feedface:collision")
    ).all()
    assert len(eval_jobs) == 2
