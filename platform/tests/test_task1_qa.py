import subprocess
import textwrap
from pathlib import Path

import pytest

from test_api import create_job
from ticloud.config import settings
from ticloud.models import Alert, Run, RunStatus
from ticloud.scheduler.queue import claim_next_run
from ticloud.scheduler.worker import execute_run

ADMIN = {"Authorization": "Bearer admin-secret"}
PLATFORM_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qa_hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys", json={"name": "qa"}, headers=ADMIN
    ).json()
    return tenant, {"Authorization": f"Bearer {key['secret']}"}


def _hold_for_approval(session, client):
    job = create_job(client, cron=None, approval_required=True)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run["id"]
    assert execute_run(claimed.id) == RunStatus.AWAITING_APPROVAL
    session.expire_all()
    assert session.get(Run, run["id"]).status == RunStatus.AWAITING_APPROVAL
    return run


def test_qa_cancel_awaiting_approval_is_terminal_and_not_requeueable(session, client):
    run = _hold_for_approval(session, client)

    resp = client.post(f"/runs/{run['id']}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["error"] == "cancelled by user"
    assert body["finished_at"] is not None

    session.expire_all()
    stored = session.get(Run, run["id"])
    assert stored.status == RunStatus.CANCELLED
    assert stored.finished_at is not None
    assert claim_next_run(session) is None
    assert client.get("/approvals").json() == []

    assert client.post(f"/runs/{run['id']}/approve").status_code == 409
    assert client.post(f"/runs/{run['id']}/reject").status_code == 409
    assert client.post(f"/runs/{run['id']}/cancel").status_code == 409


def test_qa_job_patch_rejects_unknown_field_without_partial_update(client):
    job = create_job(client, cron=None, budget_usd=1.0, payload={"kept": True})

    resp = client.patch(
        f"/jobs/{job['id']}",
        json={"budget_usd": 9.0, "payload": {"kept": False}, "budget_usdd": 9.0},
    )

    assert resp.status_code == 422
    assert "budget_usdd" in resp.text

    stored = client.get(f"/jobs/{job['id']}").json()
    assert stored["budget_usd"] == 1.0
    assert stored["payload"] == {"kept": True}


def test_qa_job_create_rejects_unknown_top_level_but_allows_payload_keys(client):
    bad = client.post(
        "/jobs",
        json={
            "name": "qa-extra-field",
            "cron": None,
            "payload": {"arbitrary": {"nested": True}},
            "approval_requiredd": True,
        },
    )
    assert bad.status_code == 422
    assert "approval_requiredd" in bad.text
    assert client.get("/jobs").json() == []

    good = client.post(
        "/jobs",
        json={
            "name": "qa-payload-keys",
            "cron": None,
            "payload": {"arbitrary": {"nested": True}},
        },
    )
    assert good.status_code == 201, good.text
    assert good.json()["payload"] == {"arbitrary": {"nested": True}}


def test_qa_ack_all_counts_only_currently_open_alerts_and_keeps_filter_limits(client, session):
    job = create_job(client, cron=None)
    session.add_all(
        [
            Alert(job_id=job["id"], kind="low_score", message="open-a"),
            Alert(job_id=job["id"], kind="run_failed", message="open-b"),
            Alert(
                job_id=job["id"],
                kind="auto_paused",
                message="already-acked",
                acknowledged=True,
            ),
        ]
    )
    session.commit()

    assert client.get("/alerts?limit=0").status_code == 422
    assert client.get("/alerts?limit=501").status_code == 422

    limited_open = client.get("/alerts?acknowledged=false&limit=1").json()
    assert len(limited_open) == 1
    assert limited_open[0]["acknowledged"] is False

    assert client.post("/alerts/ack-all").json() == {"acknowledged": 2}
    assert client.get("/alerts?acknowledged=false").json() == []
    acked = client.get("/alerts?acknowledged=true").json()
    assert {alert["message"] for alert in acked} == {
        "open-a",
        "open-b",
        "already-acked",
    }
    assert client.post("/alerts/ack-all").json() == {"acknowledged": 0}


def test_qa_ack_all_empty_tenant_does_not_ack_foreign_alerts(client, qa_hosted, session):
    _, empty_auth = _mint_tenant(client, "empty-team")
    _, owner_auth = _mint_tenant(client, "owner-team")
    job = client.post("/jobs", json={"name": "owner-job"}, headers=owner_auth).json()
    session.add(Alert(job_id=job["id"], kind="low_score", message="owner-open"))
    session.commit()

    assert client.get("/alerts/summary", headers=empty_auth).json() == {"unacknowledged": 0}
    assert client.post("/alerts/ack-all", headers=empty_auth).json() == {"acknowledged": 0}

    assert client.get("/alerts/summary", headers=owner_auth).json() == {"unacknowledged": 1}
    owner_open = client.get("/alerts?acknowledged=false", headers=owner_auth).json()
    assert len(owner_open) == 1
    assert owner_open[0]["message"] == "owner-open"


def test_qa_alert_filter_ui_has_unknown_route_and_acked_tab_guards(client):
    app_js = client.get("/ui/app.js").text
    style_css = client.get("/ui/style.css").text

    assert 'filter = ["open", "acked"].includes(filter) ? filter : "all";' in app_js
    assert 'api("/alerts?acknowledged=false&limit=1")' in app_js
    assert 'filter !== "acked" && openAlerts.length' in app_js
    assert 'href="#/alerts/open"' in app_js
    assert 'href="#/alerts/acked"' in app_js
    assert "flex-wrap: wrap;" in style_css


def test_qa_running_cancel_flag_survives_detail_and_list_responses(session, client):
    job = create_job(client, cron=None)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    assert run["status"] == "queued"
    assert run["cancel_requested"] is False
    assert "approval_state" in run

    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run["id"]

    before = client.get(f"/runs/{run['id']}").json()
    assert before["status"] == "running"
    assert before["cancel_requested"] is False

    cancelled = client.post(f"/runs/{run['id']}/cancel").json()
    assert cancelled["status"] == "running"
    assert cancelled["cancel_requested"] is True
    assert cancelled["finished_at"] is None

    detail = client.get(f"/runs/{run['id']}").json()
    listed = client.get(f"/jobs/{job['id']}/runs").json()
    assert detail["cancel_requested"] is True
    assert len(listed) == 1
    assert listed[0]["id"] == run["id"]
    assert listed[0]["cancel_requested"] is True


def test_qa_template_form_submit_trims_payload_and_omits_blank_cron():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    const templateForms = [{
      dataset: { template: "qa template/one" },
      values: {
        name: "  patrol from template  ",
        payload_repo_url: "  https://example.test/repo  ",
        cron: " \n\t ",
      },
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
    }];
    const newJobForm = { addEventListener() {} };
    const appEl = {
      innerHTML: "",
      addEventListener() {},
    };
    const alertCount = {};
    const classList = { add() {}, remove() {} };

    class FakeFormData {
      constructor(form) { this.form = form; }
      get(key) { return this.form.values[key]; }
      entries() { return Object.entries(this.form.values); }
    }

    function response(status, body) {
      return {
        ok: status >= 200 && status < 300,
        status,
        statusText: String(status),
        json: async () => body,
      };
    }

    const context = {
      console,
      Date,
      JSON,
      Math,
      Number,
      String,
      Set,
      Promise,
      encodeURIComponent,
      clearTimeout() {},
      setTimeout() { return 1; },
      localStorage: { getItem() { return null; }, setItem() {} },
      prompt() { return ""; },
      location: { hash: "#/jobs" },
      window: { addEventListener() {}, EventSource: undefined },
      FormData: FakeFormData,
      document: {
        hidden: false,
        activeElement: null,
        body: { append() {} },
        addEventListener() {},
        createElement() { return { className: "", textContent: "", classList }; },
        querySelector(selector) { return selector === ".toast" ? null : null; },
        querySelectorAll(selector) {
          return selector === "form.templatejob" ? templateForms : [];
        },
        getElementById(id) {
          if (id === "app") return appEl;
          if (id === "alert-count") return alertCount;
          if (id === "newjob") return newJobForm;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/overview") return response(200, []);
        if (path === "/templates") {
          return response(200, [{
            id: "qa template/one",
            name: "QA Template",
            engine: "ti",
            cron: "0 2 * * *",
            required_payload: ["repo_url"],
            description: "exercise the real submit handler",
          }]);
        }
        if (path === "/jobs/from-template/qa%20template%2Fone") {
          return response(201, { id: "created-job" });
        }
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));
      await Promise.resolve();

      assert.strictEqual(typeof templateForms[0].handlers.submit, "function");
      await templateForms[0].handlers.submit({
        preventDefault() {},
        target: templateForms[0],
      });

      const post = calls.find((call) => String(call.path).startsWith("/jobs/from-template/"));
      assert(post, "template submit did not POST");
      assert.strictEqual(post.path, "/jobs/from-template/qa%20template%2Fone");
      const body = JSON.parse(post.opts.body);
      assert.deepStrictEqual(body, {
        name: "patrol from template",
        payload: { repo_url: "https://example.test/repo" },
      });
      assert.strictEqual(context.location.hash, "#/jobs/created-job");
      console.log("QA_TEMPLATE_FORM_OK", JSON.stringify(body));
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
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
    assert "QA_TEMPLATE_FORM_OK" in result.stdout
