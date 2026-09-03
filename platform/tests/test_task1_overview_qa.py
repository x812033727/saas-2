import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from test_scheduler import make_job
from ticloud.config import settings
from ticloud.models import Run, RunStatus


ADMIN = {"Authorization": "Bearer admin-secret"}
PLATFORM_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qa_hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys",
        json={"name": "qa"},
        headers=ADMIN,
    ).json()
    return {"Authorization": f"Bearer {key['secret']}"}


def _manual_job(client, headers, name):
    resp = client.post(
        "/jobs",
        json={"name": name, "engine": "offline", "cron": None},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_run(session, *, run_id, job_id, cost, score, scheduled_at):
    session.add(
        Run(
            id=run_id,
            job_id=job_id,
            status=RunStatus.SUCCEEDED,
            scheduled_at=scheduled_at,
            cost_usd=cost,
            score=score,
        )
    )


def test_qa_overview_recent_stats_tie_breaker_is_stable_and_limited(session, client):
    job = make_job(session, name="qa-tie-trend")
    scheduled = datetime(2026, 8, 27, 7, 0, tzinfo=timezone.utc)

    for i in range(1, 10):
        _add_run(
            session,
            run_id=f"qa_run_{i:02d}",
            job_id=job.id,
            cost=float(i),
            score=i / 10,
            scheduled_at=scheduled,
        )
    session.commit()

    rows = client.get("/overview").json()
    row = next(r for r in rows if r["name"] == "qa-tie-trend")

    assert row["last_run"]["id"] == "qa_run_09"
    assert [p["run_id"] for p in row["recent_stats"]] == [
        f"qa_run_{i:02d}" for i in range(2, 10)
    ]
    assert [p["cost_usd"] for p in row["recent_stats"]] == pytest.approx(
        [float(i) for i in range(2, 10)]
    )


def test_qa_hosted_overview_recent_stats_do_not_cross_tenants(
    session, client, qa_hosted
):
    auth_a = _mint_tenant(client, "qa-team-a")
    auth_b = _mint_tenant(client, "qa-team-b")
    job_a = _manual_job(client, auth_a, "same-dashboard-name")
    job_b = _manual_job(client, auth_b, "same-dashboard-name")
    base = datetime(2026, 8, 27, 7, 0, tzinfo=timezone.utc)

    for i, cost in enumerate((10.0, 11.0, 12.0), start=1):
        _add_run(
            session,
            run_id=f"qa_a_{i:02d}",
            job_id=job_a["id"],
            cost=cost,
            score=cost / 100,
            scheduled_at=base,
        )
    for i, cost in enumerate((90.0, 91.0), start=1):
        _add_run(
            session,
            run_id=f"qa_b_{i:02d}",
            job_id=job_b["id"],
            cost=cost,
            score=cost / 100,
            scheduled_at=base,
        )
    session.commit()

    row_a = client.get("/overview", headers=auth_a).json()[0]
    row_b = client.get("/overview", headers=auth_b).json()[0]

    assert row_a["last_run"]["id"] == "qa_a_03"
    assert [p["run_id"] for p in row_a["recent_stats"]] == [
        "qa_a_01",
        "qa_a_02",
        "qa_a_03",
    ]
    assert row_b["last_run"]["id"] == "qa_b_02"
    assert [p["run_id"] for p in row_b["recent_stats"]] == ["qa_b_01", "qa_b_02"]


def test_qa_jobs_attention_summary_handles_missing_and_dirty_counts():
    script = """
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    function response(status, body) {
      return { ok: status < 400, status, statusText: String(status), json: async () => body };
    }

    const app = { innerHTML: "", addEventListener() {} };
    const context = {
      console,
      setTimeout() { return 0; },
      clearTimeout() {},
      location: { hash: "#/jobs" },
      localStorage: { getItem() { return null; }, setItem() {} },
      prompt() { return ""; },
      window: { addEventListener() {} },
      document: {
        hidden: false,
        activeElement: null,
        body: { append() {} },
        getElementById() { return app; },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        createElement() {
          return { className: "", textContent: "", classList: { add() {}, remove() {} } };
        },
        addEventListener() {},
      },
      fetch: async (path) => {
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/overview") return response(200, []);
        if (path === "/templates") return response(200, []);
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    const code = fs.readFileSync("ticloud/web/app.js", "utf8");
    vm.runInNewContext(code, context, { filename: "app.js" });

    const summary = context.attentionSummary;
    assert.strictEqual(typeof summary, "function");
    assert(summary({ id: "empty-job" }).includes('class="muted"'));

    const both = summary({
      id: "job id/1",
      unacknowledged_alerts: "2",
      awaiting_approval_runs: 1,
    });
    assert(both.includes("#/alerts/open/job%20id%2F1"));
    assert(both.includes("2 alerts"));
    assert(both.includes("#/approvals/job%20id%2F1"));
    assert(both.includes("1 approval"));

    const dirty = summary({
      id: "dirty",
      unacknowledged_alerts: "not-a-number",
      awaiting_approval_runs: "1",
    });
    assert(!dirty.includes("NaN"));
    assert(!dirty.includes("not-a-number"));
    assert(!dirty.includes("alert"));
    assert(dirty.includes("1 approval"));
    console.log("QA_ATTENTION_SUMMARY_OK");
    """

    result = subprocess.run(
        ["node", "-e", textwrap.dedent(script)],
        cwd=PLATFORM_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_ATTENTION_SUMMARY_OK" in result.stdout
