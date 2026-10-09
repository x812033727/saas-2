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
        scorers: '{"judge":{"enabled":true}}',
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
        scorers: { judge: { enabled: true } },
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
        engine: "ti",
        cron: "",
        interval_seconds: "30",
        payload: "{bad json",
        budget_usd: "9.25",
        timeout_s: "240",
        max_retries: "4",
        retry_backoff_s: "20",
        score_threshold: "",
        on_low_score: "alert",
        scorers: "{bad json",
        webhook_url: "   ",
        paused: "on",
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
      scorers: { completion: { enabled: true } },
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
        if (path === "/jobs/job-1/runs?limit=50") return response(200, []);
        if (path === "/jobs/job-1/stats") return response(200, []);
        if (path === "/jobs/job-1/lessons") return response(200, []);
        if (path === "/failure-modes?job_id=job-1") return response(200, []);
        if (path === "/eval-cases?job_id=job-1") return response(200, []);
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
      assert.strictEqual(
        calls.filter((call) => call.path === "/jobs/job-1" && call.opts.method === "PATCH").length,
        0,
        "invalid settings scorers JSON should not PATCH the job",
      );
      assert.strictEqual(toastEl.textContent, "scorers must be a JSON object");

      jobSettingsForm.values.scorers = '{"judge":{"enabled":true}}';
      await jobSettingsForm.handlers.submit({ preventDefault() {}, target: jobSettingsForm });

      const patches = calls.filter((call) => call.path === "/jobs/job-1" && call.opts.method === "PATCH");
      assert.strictEqual(patches.length, 1);
      const body = JSON.parse(patches[0].opts.body);
      assert.deepStrictEqual(body, {
        name: "qa-updated",
        engine: "ti",
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
        scorers: { judge: { enabled: true } },
        webhook_url: null,
        paused: true,
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


def test_qa_job_detail_survives_secondary_api_failures():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    const jobSettingsForm = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
    const lessonForm = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
    const jobEvalCaseForm = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
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
      name: "qa-secondary-fallback",
      engine: "offline",
      paused: false,
      cron: null,
      interval_seconds: 600,
      payload: { repo_url: "https://example.test/repo" },
      budget_usd: 5,
      timeout_s: 1800,
      max_retries: 2,
      retry_backoff_s: 0,
      score_threshold: 0.75,
      on_low_score: "alert",
      webhook_url: null,
      approval_required: false,
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
          if (id === "jobevalcase") return jobEvalCaseForm;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/jobs/job-1") return response(200, job);
        if (path === "/jobs/job-1/schedule-preview?count=5") {
          return response(200, { paused: false, next_run_at: nextRunAt, upcoming: [nextRunAt] });
        }
        if (path === "/jobs/job-1/runs?limit=50") return response(500, { detail: "runs exploded" });
        if (path === "/jobs/job-1/runs") return response(500, { detail: "legacy runs exploded" });
        if (path === "/jobs/job-1/stats") return response(500, { detail: "stats exploded" });
        if (path === "/jobs/job-1/lessons") return response(500, { detail: "lessons exploded" });
        if (path === "/failure-modes?job_id=job-1") return response(500, { detail: "modes exploded" });
        if (path === "/eval-cases?job_id=job-1") return response(500, { detail: "cases exploded" });
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert(appEl.innerHTML.includes("qa-secondary-fallback"));
      assert(appEl.innerHTML.includes("No lessons yet"));
      assert(appEl.innerHTML.includes("No failed runs for this job yet."));
      assert(appEl.innerHTML.includes("No regression eval cases for this job yet."));
      assert(appEl.innerHTML.includes("No runs yet"));
      assert(!appEl.innerHTML.includes("exploded"));
      assert.strictEqual(typeof jobSettingsForm.handlers.submit, "function");
      assert.strictEqual(typeof lessonForm.handlers.submit, "function");
      assert.strictEqual(typeof jobEvalCaseForm.handlers.submit, "function");
      assert(calls.some((call) => call.path === "/jobs/job-1/runs?limit=50"));
      assert(calls.some((call) => call.path === "/jobs/job-1/runs"));
      console.log("QA_JOB_DETAIL_SECONDARY_API_FALLBACK_OK");
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_JOB_DETAIL_SECONDARY_API_FALLBACK_OK" in result.stdout


def test_qa_filtered_run_history_failure_does_not_leak_unfiltered_runs():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    const jobSettingsForm = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
    const lessonForm = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
    const jobEvalCaseForm = { handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } };
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

    const nextRunAt = new Date(Date.now() + 900000).toISOString();
    const job = {
      id: "job-1",
      name: "qa-filtered-history",
      engine: "offline",
      paused: false,
      cron: null,
      interval_seconds: 600,
      payload: { repo_url: "https://example.test/repo" },
      budget_usd: 5,
      timeout_s: 1800,
      max_retries: 2,
      retry_backoff_s: 0,
      score_threshold: 0.75,
      on_low_score: "alert",
      webhook_url: null,
      approval_required: false,
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
      location: { hash: "#/jobs/job-1/failed" },
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
          if (id === "jobevalcase") return jobEvalCaseForm;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/jobs/job-1") return response(200, job);
        if (path === "/jobs/job-1/schedule-preview?count=5") {
          return response(200, { paused: false, upcoming: [nextRunAt] });
        }
        if (path === "/jobs/job-1/runs?limit=50&status=failed") {
          return response(500, { detail: "filtered history exploded" });
        }
        if (path === "/jobs/job-1/runs") {
          return response(200, [{
            id: "all-run-should-not-leak",
            status: "succeeded",
            attempt: 1,
            scheduled_at: nextRunAt,
            started_at: nextRunAt,
            finished_at: nextRunAt,
            score: 1,
            cost_usd: 0,
            result: { summary: "wrong filter" },
          }]);
        }
        if (path === "/jobs/job-1/stats") return response(200, []);
        if (path === "/jobs/job-1/lessons") return response(200, []);
        if (path === "/failure-modes?job_id=job-1") return response(200, []);
        if (path === "/eval-cases?job_id=job-1") return response(200, []);
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert(appEl.innerHTML.includes("qa-filtered-history"));
      assert(appEl.innerHTML.includes("No runs match this filter."));
      assert(!appEl.innerHTML.includes("all-run-should-not-leak"));
      assert(calls.some((call) => call.path === "/jobs/job-1/runs?limit=50&status=failed"));
      assert(!calls.some((call) => call.path === "/jobs/job-1/runs"));
      assert.strictEqual(typeof jobSettingsForm.handlers.submit, "function");
      assert.strictEqual(typeof lessonForm.handlers.submit, "function");
      assert.strictEqual(typeof jobEvalCaseForm.handlers.submit, "function");
      console.log("QA_FILTERED_RUN_HISTORY_FAIL_CLOSED_OK");
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_FILTERED_RUN_HISTORY_FAIL_CLOSED_OK" in result.stdout


def test_qa_job_detail_eval_case_form_posts_current_job_and_blocks_bad_json():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    let toastEl = null;
    const jobEvalCaseForm = {
      values: {
        name: "  qa-job-regression  ",
        engine: "ti",
        min_score: "0.65",
        payload: "[1,2,3]",
      },
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
    };
    const jobSettingsForm = { addEventListener() {} };
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
      name: "qa-job",
      engine: "offline",
      paused: false,
      cron: null,
      interval_seconds: null,
      payload: { repo_url: "https://example.test/repo", branch: "main" },
      budget_usd: 5,
      timeout_s: 1800,
      max_retries: 2,
      retry_backoff_s: 0,
      score_threshold: 0.8,
      on_low_score: "alert",
      webhook_url: null,
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
          if (id === "jobevalcase") return jobEvalCaseForm;
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
        if (path === "/eval-cases" && opts.method === "POST") {
          return response(201, { id: "case-1" });
        }
        if (path === "/eval-cases?job_id=job-1") return response(200, []);
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert.strictEqual(typeof jobEvalCaseForm.handlers.submit, "function");
      await jobEvalCaseForm.handlers.submit({ preventDefault() {}, target: jobEvalCaseForm });
      assert.strictEqual(
        calls.filter((call) => call.path === "/eval-cases" && call.opts.method === "POST").length,
        0,
        "invalid payload JSON should not create a job-scoped eval case",
      );
      assert.strictEqual(toastEl.textContent, "payload must be a JSON object");

      jobEvalCaseForm.values.payload = '{"repo_url":"https://example.test/override","steps":["qa"]}';
      await jobEvalCaseForm.handlers.submit({ preventDefault() {}, target: jobEvalCaseForm });

      const posts = calls.filter((call) => call.path === "/eval-cases" && call.opts.method === "POST");
      assert.strictEqual(posts.length, 1);
      const body = JSON.parse(posts[0].opts.body);
      assert.deepStrictEqual(body, {
        name: "qa-job-regression",
        engine: "ti",
        min_score: 0.65,
        payload: {
          repo_url: "https://example.test/override",
          steps: ["qa"],
        },
        job_id: "job-1",
      });
      assert.strictEqual(context.location.hash, "#/jobs/job-1");
      console.log("QA_JOB_EVAL_CASE_FORM_OK", JSON.stringify(body));
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_JOB_EVAL_CASE_FORM_OK" in result.stdout


def test_qa_run_detail_eval_case_form_posts_run_job_and_redirects_after_success():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
    let toastEl = null;
    const runEvalCaseForm = {
      values: {
        name: "  qa-run-regression  ",
        engine: "offline",
        min_score: "0.72",
        payload: "[]",
      },
      handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
    };
    const jobEvalCaseForm = { addEventListener() {} };
    const jobSettingsForm = { addEventListener() {} };
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
      name: "qa-run-job",
      engine: "offline",
      paused: false,
      cron: null,
      interval_seconds: null,
      payload: { repo_url: "https://example.test/run-source", prompt: "debug me" },
      budget_usd: 5,
      timeout_s: 1800,
      max_retries: 2,
      retry_backoff_s: 0,
      score_threshold: 0.72,
      on_low_score: "alert",
      webhook_url: null,
      approval_required: false,
      next_run_at: null,
    };
    const run = {
      id: "run-abcdef123456",
      job_id: "job-1",
      status: "failed",
      attempt: 2,
      scheduled_at: "2026-10-03T00:00:00Z",
      started_at: "2026-10-03T00:00:01Z",
      finished_at: "2026-10-03T00:00:03Z",
      score: 0.2,
      cost_usd: 0.0123,
      tokens_in: 12,
      tokens_out: 34,
      steps: [],
      scores: [],
      result: null,
      error: "boom",
      cancel_requested: false,
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
      location: { hash: "#/runs/run-abcdef123456" },
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
          if (id === "runevalcase") return runEvalCaseForm;
          if (id === "jobsettings") return jobSettingsForm;
          if (id === "lessonform") return lessonForm;
          if (id === "jobevalcase") return jobEvalCaseForm;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/runs/run-abcdef123456") return response(200, run);
        if (path === "/jobs/job-1") return response(200, job);
        if (path === "/jobs/job-1/lessons") return response(200, []);
        if (path === "/eval-cases" && opts.method === "POST") {
          return response(201, { id: "case-from-run" });
        }
        if (path === "/jobs/job-1/runs") return response(200, []);
        if (path === "/jobs/job-1/stats") return response(200, []);
        if (path === "/failure-modes?job_id=job-1") return response(200, []);
        if (path === "/eval-cases?job_id=job-1") return response(200, []);
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert.strictEqual(typeof runEvalCaseForm.handlers.submit, "function");
      await runEvalCaseForm.handlers.submit({ preventDefault() {}, target: runEvalCaseForm });
      assert.strictEqual(
        calls.filter((call) => call.path === "/eval-cases" && call.opts.method === "POST").length,
        0,
        "invalid payload JSON should not create a run-scoped eval case",
      );
      assert.strictEqual(toastEl.textContent, "payload must be a JSON object");

      runEvalCaseForm.values.payload = '{"repo_url":"https://example.test/run-source","prompt":"replay failure"}';
      await runEvalCaseForm.handlers.submit({ preventDefault() {}, target: runEvalCaseForm });

      const posts = calls.filter((call) => call.path === "/eval-cases" && call.opts.method === "POST");
      assert.strictEqual(posts.length, 1);
      const body = JSON.parse(posts[0].opts.body);
      assert.deepStrictEqual(body, {
        name: "qa-run-regression",
        engine: "offline",
        min_score: 0.72,
        payload: {
          repo_url: "https://example.test/run-source",
          prompt: "replay failure",
        },
        job_id: "job-1",
      });
      assert.strictEqual(context.location.hash, "#/jobs/job-1");
      console.log("QA_RUN_EVAL_CASE_FORM_OK", JSON.stringify(body));
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_RUN_EVAL_CASE_FORM_OK" in result.stdout
