import subprocess
import textwrap
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]


def _run_node(script):
    return subprocess.run(
        ["node", "-e", textwrap.dedent(script)],
        cwd=PLATFORM_ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_qa_custom_job_form_posts_payload_retries_and_webhook_and_blocks_bad_json():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    let toastEl = null;
    const newJobForm = {
      values: {
        name: "qa-ui-job",
        engine: "ti",
        cron: "",
        interval_seconds: "",
        payload: "[1,2,3]",
        budget_usd: "7.50",
        timeout_s: "120",
        max_retries: "3",
        retry_backoff_s: "15",
        score_threshold: "0.8",
        on_low_score: "pause",
        webhook_url: "  https://hooks.example/qa  ",
        approval_required: "on",
      },
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
    };
    const appEl = { innerHTML: "", addEventListener() {} };
    const alertCount = {};

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
          return selector === "form.templatejob" ? [] : [];
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
        if (path === "/templates") return response(200, []);
        if (path === "/jobs") return response(201, { id: "created-job" });
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert.strictEqual(typeof newJobForm.handlers.submit, "function");
      await newJobForm.handlers.submit({ preventDefault() {}, target: newJobForm });
      assert.strictEqual(
        calls.filter((call) => call.path === "/jobs" && call.opts.method === "POST").length,
        0,
        "invalid payload JSON should not POST a job",
      );
      assert.strictEqual(toastEl.textContent, "payload must be a JSON object");

      newJobForm.values.payload = '{"repo_url":"https://example.test/repo","nested":{"step":2}}';
      await newJobForm.handlers.submit({ preventDefault() {}, target: newJobForm });

      const posts = calls.filter((call) => call.path === "/jobs" && call.opts.method === "POST");
      assert.strictEqual(posts.length, 1);
      const body = JSON.parse(posts[0].opts.body);
      assert.deepStrictEqual(body, {
        name: "qa-ui-job",
        engine: "ti",
        payload: {
          repo_url: "https://example.test/repo",
          nested: { step: 2 },
        },
        budget_usd: 7.5,
        timeout_s: 120,
        max_retries: 3,
        retry_backoff_s: 15,
        score_threshold: 0.8,
        on_low_score: "pause",
        webhook_url: "https://hooks.example/qa",
        approval_required: true,
      });
      assert(!Object.prototype.hasOwnProperty.call(body, "cron"));
      assert(!Object.prototype.hasOwnProperty.call(body, "interval_seconds"));
      console.log("QA_CUSTOM_JOB_FORM_OK", JSON.stringify(body));
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_CUSTOM_JOB_FORM_OK" in result.stdout


def test_qa_job_settings_form_patches_payload_and_clears_blank_optionals():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    let toastEl = null;
    const jobSettingsForm = {
      values: {
        name: "  qa-updated  ",
        cron: "",
        interval_seconds: "30",
        payload: "{bad json",
        budget_usd: "9.25",
        timeout_s: "240",
        max_retries: "4",
        retry_backoff_s: "20",
        score_threshold: "",
        on_low_score: "alert",
        webhook_url: "   ",
        approval_required: undefined,
      },
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
    };
    const lessonForm = { addEventListener() {} };
    const appEl = { innerHTML: "", addEventListener() {} };
    const alertCount = {};

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

    const job = {
      id: "job-1",
      name: "qa-original",
      engine: "offline",
      paused: false,
      cron: "0 2 * * *",
      interval_seconds: null,
      payload: { old: true },
      budget_usd: 5,
      timeout_s: 1800,
      max_retries: 2,
      retry_backoff_s: 0,
      score_threshold: 0.7,
      on_low_score: "pause",
      webhook_url: "https://hooks.example/old",
      approval_required: false,
      next_run_at: null,
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
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/jobs/job-1") return response(200, job);
        if (path === "/jobs/job-1/runs") return response(200, []);
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

      assert.strictEqual(typeof jobSettingsForm.handlers.submit, "function");
      await jobSettingsForm.handlers.submit({ preventDefault() {}, target: jobSettingsForm });
      assert.strictEqual(
        calls.filter((call) => call.path === "/jobs/job-1" && call.opts.method === "PATCH").length,
        0,
        "invalid settings payload JSON should not PATCH the job",
      );
      assert.strictEqual(toastEl.textContent, "payload must be a JSON object");

      jobSettingsForm.values.payload = '{"fail_at":2,"nested":{"step":"review"}}';
      await jobSettingsForm.handlers.submit({ preventDefault() {}, target: jobSettingsForm });

      const patches = calls.filter((call) => call.path === "/jobs/job-1" && call.opts.method === "PATCH");
      assert.strictEqual(patches.length, 1);
      const body = JSON.parse(patches[0].opts.body);
      assert.deepStrictEqual(body, {
        name: "qa-updated",
        payload: {
          fail_at: 2,
          nested: { step: "review" },
        },
        cron: null,
        interval_seconds: 30,
        budget_usd: 9.25,
        timeout_s: 240,
        max_retries: 4,
        retry_backoff_s: 20,
        score_threshold: null,
        on_low_score: "alert",
        webhook_url: null,
        approval_required: false,
      });
      console.log("QA_JOB_SETTINGS_FORM_OK", JSON.stringify(body));
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_JOB_SETTINGS_FORM_OK" in result.stdout
