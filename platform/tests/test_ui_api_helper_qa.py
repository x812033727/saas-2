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


def test_qa_api_helper_merges_custom_headers_with_tenant_auth():
    script = r"""
    const assert = require("assert");
    const fs = require("fs");
    const vm = require("vm");

    const calls = [];
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
      localStorage: {
        getItem(key) { return key === "ticloud_api_key" ? "tck_secret" : null; },
        setItem() {},
      },
      prompt() { return ""; },
      location: { hash: "#/jobs" },
      window: { addEventListener() {}, EventSource: undefined },
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
        querySelectorAll() { return []; },
        getElementById(id) {
          if (id === "app") return appEl;
          if (id === "alert-count") return alertCount;
          return null;
        },
      },
      fetch: async (path, opts = {}) => {
        calls.push({ path, opts });
        if (path === "/alerts/summary") return response(200, { unacknowledged: 0 });
        if (path === "/overview") return response(200, []);
        if (path === "/templates") return response(200, []);
        if (path === "/custom") return response(200, { ok: true });
        return response(404, { detail: `unexpected ${path}` });
      },
    };
    context.globalThis = context;

    (async () => {
      const code = fs.readFileSync("ticloud/web/app.js", "utf8");
      vm.runInNewContext(code, context, { filename: "app.js" });
      await new Promise((resolve) => setImmediate(resolve));

      assert.strictEqual(typeof context.api, "function");
      await context.api("/custom", {
        method: "POST",
        headers: { "x-extra": "1" },
        body: "{}",
      });

      const customCall = calls.find((call) => call.path === "/custom");
      assert(customCall, "custom API call was not made");
      assert.strictEqual(customCall.opts.method, "POST");
      assert.strictEqual(customCall.opts.body, "{}");
      assert.strictEqual(customCall.opts.headers["content-type"], "application/json");
      assert.strictEqual(customCall.opts.headers["x-extra"], "1");
      assert.strictEqual(customCall.opts.headers.authorization, "Bearer tck_secret");
      console.log("QA_API_HEADER_MERGE_OK");
    })().catch((err) => {
      console.error(err && err.stack ? err.stack : err);
      process.exit(1);
    });
    """

    result = _run_node(script)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_API_HEADER_MERGE_OK" in result.stdout
