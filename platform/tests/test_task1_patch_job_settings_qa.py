import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]


def _create_interval_job(client, **overrides):
    body = {
        "name": "qa-patch-job",
        "engine": "offline",
        "interval_seconds": 600,
        "payload": {"mode": "initial"},
    }
    body.update(overrides)
    resp = client.post("/jobs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_patch_job_engine_and_resume_keeps_history_and_reanchors(client, session):
    from ticloud.models import Job

    job = _create_interval_job(client)
    run = client.post(f"/jobs/{job['id']}/trigger").json()

    db_job = session.get(Job, job["id"])
    db_job.paused = True
    db_job.next_run_at = datetime.now(timezone.utc) - timedelta(days=2)
    session.commit()

    resp = client.patch(
        f"/jobs/{job['id']}",
        json={
            "engine": "ti",
            "payload": {
                "repo_url": "https://github.com/acme/qa-target",
                "brief": "resume with a new engine",
            },
            "paused": False,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["engine"] == "ti"
    assert body["paused"] is False
    assert datetime.fromisoformat(body["next_run_at"]) > datetime.now(timezone.utc)
    assert client.get(f"/jobs/{job['id']}/runs").json()[0]["id"] == run["id"]


def test_patch_paused_job_does_not_enqueue_due_schedule(client, session):
    from ticloud.models import Job
    from ticloud.scheduler.queue import enqueue_due_jobs

    job = _create_interval_job(client)
    now = datetime.now(timezone.utc)
    db_job = session.get(Job, job["id"])
    db_job.next_run_at = now - timedelta(minutes=5)
    session.commit()

    resp = client.patch(f"/jobs/{job['id']}", json={"paused": True})

    assert resp.status_code == 200, resp.text
    assert resp.json()["paused"] is True
    session.expire_all()
    assert enqueue_due_jobs(session, now=now) == []
    assert session.get(Job, job["id"]).paused is True


def test_patch_empty_body_is_noop_and_does_not_reanchor_stale_schedule(client, session):
    from ticloud.models import Job

    job = _create_interval_job(client)
    stale_next_run = datetime.now(timezone.utc) - timedelta(hours=3)
    db_job = session.get(Job, job["id"])
    db_job.next_run_at = stale_next_run
    session.commit()

    resp = client.patch(f"/jobs/{job['id']}", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["engine"] == "offline"
    assert body["paused"] is False
    assert datetime.fromisoformat(body["next_run_at"]) == stale_next_run


def test_ui_job_settings_unchecked_paused_posts_false():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    let toastEl = null;
    const jobSettingsForm = {
      values: {
        name: "qa-active-job",
        engine: "offline",
        cron: "",
        interval_seconds: "600",
        payload: '{"mode":"edited"}',
        budget_usd: "5",
        timeout_s: "60",
        max_retries: "1",
        retry_backoff_s: "0",
        score_threshold: "",
        on_low_score: "alert",
        scorers: "{}",
        webhook_url: "",
        paused: undefined,
        approval_required: undefined,
      },
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
    };
    const appEl = { innerHTML: "", addEventListener() {} };
    const alertCount = {};
    const lessonForm = { addEventListener() {} };
    const jobEvalCaseForm = { addEventListener() {} };

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

    const nextRunAt = new Date(Date.now() + 600000).toISOString();
    const job = {
      id: "job-1",
      name: "qa-active-job",
      engine: "offline",
      paused: true,
      cron: null,
      interval_seconds: 600,
      payload: { mode: "initial" },
      budget_usd: 5,
      timeout_s: 60,
      max_retries: 1,
      retry_backoff_s: 0,
      score_threshold: null,
      on_low_score: "alert",
      webhook_url: null,
      approval_required: false,
      next_run_at: nextRunAt,
      created_at: nextRunAt,
      scorers: {},
    };

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
      decodeURIComponent,
      clearTimeout() {},
      setTimeout() { return 1; },
      localStorage: { getItem() { return null; }, setItem() {} },
      prompt() { return ""; },
      location: { hash: "#/jobs/job-1" },
      window: { addEventListener() {}, EventSource: undefined },
      FormData: FakeFormData,
      document: {
        hidden: false,
        activeElement: null,
        body: { append() {} },
        addEventListener() {},
        createElement() {
          toastEl = {
            className: "",
            textContent: "",
            classList: { add() {}, remove() {} },
          };
          return toastEl;
        },
        querySelector(selector) { return selector === ".toast" ? toastEl : null; },
        querySelectorAll(selector) {
          return selector === ".lessonedit" ? [] : [];
        },
        getElementById(id) {
          if (id === "app") return appEl;
          if (id === "alert-count") return alertCount;
          if (id === "jobsettings") return jobSettingsForm;
          if (id === "lessonform") return lessonForm;
          if (id === "jobevalcase") return jobEvalCaseForm;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/jobs/job-1") return response(200, job);
        if (path === "/jobs/job-1/schedule-preview?count=5") {
          return response(200, { paused: true, next_run_at: nextRunAt, upcoming: [nextRunAt] });
        }
        if (path === "/jobs/job-1/runs?limit=50") return response(200, []);
        if (path === "/jobs/job-1/stats") return response(200, []);
        if (path === "/jobs/job-1/lessons") return response(200, []);
        if (path === "/failure-modes?job_id=job-1") return response(200, []);
        if (path === "/eval-cases?job_id=job-1") return response(200, []);
        if (path === "/jobs/job-1" && opts.method === "PATCH") return response(200, { ...job, paused: false });
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert.strictEqual(typeof jobSettingsForm.handlers.submit, "function");
      await jobSettingsForm.handlers.submit({ preventDefault() {}, target: jobSettingsForm });

      const patches = calls.filter((call) => call.path === "/jobs/job-1" && call.opts.method === "PATCH");
      assert.strictEqual(patches.length, 1);
      const body = JSON.parse(patches[0].opts.body);
      assert.strictEqual(body.paused, false);
      assert.strictEqual(body.approval_required, false);
      assert.strictEqual(body.interval_seconds, 600);
      assert.strictEqual(body.cron, null);
      console.log("QA_JOB_SETTINGS_UNCHECKED_PAUSED_FALSE", JSON.stringify(body));
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
    assert "QA_JOB_SETTINGS_UNCHECKED_PAUSED_FALSE" in result.stdout
