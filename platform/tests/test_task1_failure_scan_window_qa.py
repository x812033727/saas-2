import json
import subprocess
import textwrap
from pathlib import Path


def test_qa_failure_modes_rejects_scan_window_above_contract(client):
    valid = client.get("/failure-modes?limit_runs=5000")
    assert valid.status_code == 200, valid.text

    too_large = client.get("/failure-modes?limit_runs=5001")
    assert too_large.status_code == 422


def test_qa_failure_route_helpers_preserve_filters_and_reject_bad_windows():
    root = Path(__file__).resolve().parents[1]
    app_js = root / "ticloud" / "web" / "app.js"
    script = textwrap.dedent(
        f"""
        const assert = require("assert");
        const fs = require("fs");
        const vm = require("vm");
        const app = fs.readFileSync({json.dumps(str(app_js))}, "utf8");
        const start = app.indexOf("function failureRoute");
        const end = app.indexOf("function evalCaseRows");
        assert(start >= 0 && end > start, "failure route helpers not found");

        const context = {{}};
        vm.createContext(context);
        vm.runInContext(app.slice(start, end), context);
        const route = (...args) => JSON.parse(JSON.stringify(context.parseFailureRoute(...args)));

        assert.strictEqual(context.failureRoute("all", 500), "#/failures");
        assert.strictEqual(context.failureRoute("all", 1000), "#/failures/last-1000");
        assert.strictEqual(
          context.failureRoute("unpromoted", 5000),
          "#/failures/unpromoted/last-5000",
        );
        assert.deepStrictEqual(
          route("last-1000"),
          {{ filter: "all", limitRuns: 1000 }},
        );
        assert.deepStrictEqual(
          route("recurring", "last-5000"),
          {{ filter: "recurring", limitRuns: 5000 }},
        );
        assert.deepStrictEqual(
          route("unpromoted", "last-1"),
          {{ filter: "unpromoted", limitRuns: 500 }},
        );
        assert.strictEqual(
          context.failureRoute("category-rate_limit", 100),
          "#/failures/category-rate_limit/last-100",
        );
        assert.strictEqual(
          vm.runInContext('failureCategoryFilter("rate_limit")', context),
          "category-rate_limit",
        );
        assert.deepStrictEqual(
          route("category-rate_limit", "last-100"),
          {{ filter: "category-rate_limit", limitRuns: 100, category: "rate_limit" }},
        );
        assert.deepStrictEqual(
          route("category-database", "last-1000"),
          {{ filter: "all", limitRuns: 500 }},
        );
        assert.deepStrictEqual(
          route("unexpected", "last-1000"),
          {{ filter: "all", limitRuns: 500 }},
        );
        """
    )

    result = subprocess.run(
        ["node", "-e", script],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
