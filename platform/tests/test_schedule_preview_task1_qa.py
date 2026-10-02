import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]


def _create_interval_job(client, seconds: int = 900):
    resp = client.post(
        "/jobs",
        json={
            "name": "qa-schedule-preview",
            "engine": "offline",
            "interval_seconds": seconds,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run_node(script: str):
    return subprocess.run(
        ["node", "-e", textwrap.dedent(script)],
        cwd=PLATFORM_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_qa_schedule_preview_count_bounds_and_no_side_effect(client):
    job = _create_interval_job(client, seconds=600)
    original_next_run_at = job["next_run_at"]

    assert client.get(f"/jobs/{job['id']}/schedule-preview?count=0").status_code == 422
    assert client.get(f"/jobs/{job['id']}/schedule-preview?count=21").status_code == 422

    resp = client.get(f"/jobs/{job['id']}/schedule-preview?count=20")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    upcoming = [datetime.fromisoformat(value) for value in body["upcoming"]]
    assert len(upcoming) == 20
    assert upcoming[0] == datetime.fromisoformat(original_next_run_at)
    assert upcoming[-1] == upcoming[0] + timedelta(seconds=600 * 19)
    assert client.get(f"/jobs/{job['id']}").json()["next_run_at"] == original_next_run_at


def test_qa_schedule_preview_rejects_malformed_after(client):
    job = _create_interval_job(client)

    resp = client.get(
        f"/jobs/{job['id']}/schedule-preview",
        params={"after": "not-a-date"},
    )

    assert resp.status_code == 422
    assert "after" in str(resp.json()["detail"]).lower()


def test_qa_schedule_preview_preserves_paused_state_and_anchor(client):
    job = _create_interval_job(client, seconds=300)
    paused = client.post(f"/jobs/{job['id']}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True

    resp = client.get(f"/jobs/{job['id']}/schedule-preview?count=2")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    upcoming = [datetime.fromisoformat(value) for value in body["upcoming"]]
    assert body["paused"] is True
    assert body["next_run_at"] == job["next_run_at"]
    assert upcoming == [
        datetime.fromisoformat(job["next_run_at"]),
        datetime.fromisoformat(job["next_run_at"]) + timedelta(seconds=300),
    ]


def test_qa_ui_job_detail_survives_schedule_preview_failure():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    const jobSettingsForm = { addEventListener(type, handler) { this[type] = handler; } };
    const lessonForm = { addEventListener(type, handler) { this[type] = handler; } };
    const appEl = { innerHTML: "", addEventListener() {} };
    const alertCount = {};

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
      name: "qa-preview-detail",
      engine: "offline",
      paused: false,
      cron: null,
      interval_seconds: 600,
      payload: {},
      budget_usd: 5,
      timeout_s: 1800,
      max_retries: 2,
      retry_backoff_s: 0,
      approval_required: false,
      webhook_url: null,
      score_threshold: null,
      on_low_score: "alert",
      scorers: {},
      next_run_at: nextRunAt,
      created_at: nextRunAt,
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
      FormData: function() {},
      document: {
        hidden: false,
        activeElement: null,
        body: { append() {} },
        addEventListener() {},
        createElement() {
          return {
            className: "",
            textContent: "",
            classList: { add() {}, remove() {} },
          };
        },
        querySelector() { return null; },
        querySelectorAll(selector) {
          return selector === ".lessonedit" ? [] : [];
        },
        getElementById(id) {
          if (id === "app") return appEl;
          if (id === "alert-count") return alertCount;
          if (id === "jobsettings") return jobSettingsForm;
          if (id === "lessonform") return lessonForm;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/jobs/job-1") return response(200, job);
        if (path === "/jobs/job-1/schedule-preview?count=5") {
          return response(500, { detail: "preview exploded" });
        }
        if (path === "/jobs/job-1/runs?limit=50") return response(200, []);
        if (path === "/jobs/job-1/stats") return response(200, []);
        if (path === "/jobs/job-1/lessons") return response(200, []);
        if (path === "/failure-modes?job_id=job-1") return response(200, []);
        if (path === "/eval-cases") return response(200, []);
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert(calls.some((call) => call.path === "/jobs/job-1/schedule-preview?count=5"));
      assert(appEl.innerHTML.includes("qa-preview-detail"));
      assert(appEl.innerHTML.includes("Upcoming schedule"));
      assert(appEl.innerHTML.includes("Manual only"));
      assert(!appEl.innerHTML.includes("preview exploded"));
      console.log("QA_SCHEDULE_PREVIEW_UI_FALLBACK_OK");
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_SCHEDULE_PREVIEW_UI_FALLBACK_OK" in result.stdout
