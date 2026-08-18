/* Ti Cloud dashboard — no build step, hash routing, polling refresh. */

const app = document.getElementById("app");
let pollTimer = null;

/* ---------- helpers ---------- */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  // Hosted mode: attach the tenant API key; on 401 ask for one and retry.
  const headers = { "content-type": "application/json" };
  const key = localStorage.getItem("ticloud_api_key");
  if (key) headers["authorization"] = `Bearer ${key}`;
  const resp = await fetch(path, { headers, ...opts });
  if (resp.status === 401) {
    const entered = prompt("Ti Cloud API key (tck_…):", key || "");
    if (entered && entered !== key) {
      localStorage.setItem("ticloud_api_key", entered.trim());
      return api(path, opts);
    }
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = JSON.stringify((await resp.json()).detail); } catch {}
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.status === 204 ? null : resp.json();
}

function relTime(iso) {
  if (!iso) return "—";
  const d = (new Date(iso) - Date.now()) / 1000;
  const abs = Math.abs(d);
  const units = [[60, "s"], [3600, "m"], [86400, "h"], [Infinity, "d"]];
  const [div, unit] = units.find(([lim]) => abs < lim);
  const n = Math.round(abs / { s: 1, m: 60, h: 3600, d: 86400 }[unit]);
  return d < 0 ? `${n}${unit} ago` : `in ${n}${unit}`;
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const fmtMoney = (v) => `$${(v ?? 0).toFixed(4)}`;
const formValue = (v) => esc(v ?? "");

function duration(start, end) {
  if (!start) return "—";
  const s = ((end ? new Date(end) : new Date()) - new Date(start)) / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

const STATUS_LABEL = {
  succeeded: "✓ succeeded", failed: "✕ failed", running: "● running",
  queued: "◌ queued", timed_out: "⏱ timed out",
  budget_exceeded: "$ over budget", cancelled: "− cancelled",
  awaiting_approval: "⏸ awaiting approval",
};
const TERMINAL_RUN_STATUSES = new Set([
  "succeeded", "failed", "timed_out", "budget_exceeded", "cancelled",
]);
const badge = (status) =>
  `<span class="badge ${esc(status)}"><span class="dot"></span>${esc(STATUS_LABEL[status] || status)}</span>`;

function scheduleText(job) {
  if (job.paused) return "paused";
  if (job.cron) return `cron ${job.cron}`;
  if (job.interval_seconds) return `every ${job.interval_seconds}s`;
  return "manual only";
}

function templateForm(t) {
  const fields = (t.required_payload || []).map((key) => `
    <label>${esc(key)} <input name="payload_${esc(key)}" required placeholder="${esc(key)}"></label>`).join("");
  return `
    <section class="template-item">
      <div>
        <strong>${esc(t.name)}</strong>
        <small>${esc(t.engine)} · ${esc(scheduleText(t))}</small>
        <p>${esc(t.description)}</p>
      </div>
      <form class="templatejob" data-template="${esc(t.id)}">
        <label>name <input name="name" required value="${esc(t.id)}"></label>
        ${fields}
        <label>cron override <input name="cron" placeholder="${esc(t.cron || "")}"></label>
        <button class="primary submit" type="submit">Create</button>
      </form>
    </section>`;
}

function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) { el = document.createElement("div"); el.className = "toast"; document.body.append(el); }
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2200);
}

async function act(method, path, refresh) {
  try { await api(path, { method }); toast("done"); }
  catch (e) { toast(e.message); }
  refresh();
}

/* ---------- sparkline (single series -> series-1; no legend needed) ---------- */

function sparkline(points, { value, format, label, max = null, threshold = null } = {}) {
  const pts = points.filter((p) => value(p) != null);
  if (pts.length < 2) return `<div class="empty">not enough runs for a trend yet</div>`;
  const W = 640, H = 72, PAD = 8;
  const vals = pts.map(value);
  const vmax = max ?? Math.max(...vals, 1e-9);
  const x = (i) => PAD + (i * (W - 2 * PAD)) / (pts.length - 1);
  const y = (v) => H - PAD - (Math.min(v, vmax) / vmax) * (H - 2 * PAD);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(value(p)).toFixed(1)}`).join(" ");
  const dots = pts.map((p, i) => `
    <circle class="pt" cx="${x(i).toFixed(1)}" cy="${y(value(p)).toFixed(1)}" r="3">
      <title>${esc(p.status)} · ${format(value(p))} · ${esc(fmtTime(p.scheduled_at))}</title>
    </circle>`).join("");
  const gate = threshold != null ? `
    <line class="threshold" x1="${PAD}" y1="${y(threshold).toFixed(1)}" x2="${W - PAD}" y2="${y(threshold).toFixed(1)}"/>
    <text class="threshold-label" x="${W - PAD}" y="${(y(threshold) - 4).toFixed(1)}" text-anchor="end">gate ${format(threshold)}</text>` : "";
  return `
    <svg class="spark" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}, most recent ${pts.length} runs">
      <line class="baseline" x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}"/>${gate}
      <path class="line" d="${path}"/>${dots}
    </svg>`;
}

const fmtScore = (v) => (v == null ? "—" : v.toFixed(2));

/* ---------- views ---------- */

function jobSettingsForm(job) {
  return `
    <details class="panel">
      <summary>Settings</summary>
      <div class="card">
        <form class="jobsettings" id="jobsettings">
          <label>name <input name="name" required value="${formValue(job.name)}"></label>
          <label>cron (optional) <input name="cron" placeholder="0 2 * * *" value="${formValue(job.cron)}"></label>
          <label>interval seconds (optional) <input name="interval_seconds" type="number" min="10" value="${formValue(job.interval_seconds)}"></label>
          <label>budget USD <input name="budget_usd" type="number" step="0.01" min="0.01" value="${formValue(job.budget_usd)}"></label>
          <label>timeout s <input name="timeout_s" type="number" min="1" value="${formValue(job.timeout_s)}"></label>
          <label>max retries <input name="max_retries" type="number" min="0" value="${formValue(job.max_retries)}"></label>
          <label>retry backoff s <input name="retry_backoff_s" type="number" min="0" value="${formValue(job.retry_backoff_s)}"></label>
          <label>quality gate (0–1, optional) <input name="score_threshold" type="number" step="0.05" min="0" max="1" value="${formValue(job.score_threshold)}"></label>
          <label>on low score <select name="on_low_score">
            <option value="alert" ${job.on_low_score === "alert" ? "selected" : ""}>alert</option>
            <option value="pause" ${job.on_low_score === "pause" ? "selected" : ""}>pause</option>
          </select></label>
          <label>webhook URL (optional) <input name="webhook_url" type="url" value="${formValue(job.webhook_url)}"></label>
          <label class="checkrow"><input name="approval_required" type="checkbox" ${job.approval_required ? "checked" : ""}> approval required</label>
          <button class="primary submit" type="submit">Save settings</button>
        </form>
      </div>
    </details>`;
}

async function jobsView() {
  const [jobs, templates] = await Promise.all([
    api("/overview"),
    api("/templates").catch(() => []),
  ]);
  const rows = jobs.map((j) => `
    <tr class="rowlink" data-href="#/jobs/${j.id}">
      <td><strong>${esc(j.name)}</strong><br><small style="color:var(--muted)">${esc(j.engine)}</small></td>
      <td>${esc(scheduleText(j))}<br><small style="color:var(--muted)">next ${relTime(j.next_run_at)}</small></td>
      <td>${j.last_run ? badge(j.last_run.status) : '<span style="color:var(--muted)">never ran</span>'}
          ${j.last_run ? `<br><small style="color:var(--muted)">${relTime(j.last_run.scheduled_at)}</small>` : ""}</td>
      <td class="num">${j.last_run ? fmtScore(j.last_run.score) : "—"}
          ${j.score_threshold != null ? `<br><small style="color:var(--muted)">gate ${fmtScore(j.score_threshold)}</small>` : ""}</td>
      <td class="num">${j.last_run ? fmtMoney(j.last_run.cost_usd) : "—"}</td>
      <td class="actions" data-noclick>
        <button data-act="trigger" data-id="${j.id}">Run now</button>
        <button data-act="${j.paused ? "resume" : "pause"}" data-id="${j.id}">${j.paused ? "Resume" : "Pause"}</button>
      </td>
    </tr>`).join("");

  app.innerHTML = `
    <h1>Jobs</h1>
    <div class="sub">scheduled agent workshops, guarded by budget & timeout</div>
    <div class="card">
      ${jobs.length ? `<table>
        <thead><tr><th>Job</th><th>Schedule</th><th>Last run</th><th class="num">Score</th><th class="num">Last cost</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">No jobs yet — create your first one below.</div>`}
    </div>
    <details class="panel" ${jobs.length ? "" : "open"}>
      <summary>＋ New job</summary>
      <div class="card">
        ${templates.length ? `
          <h2>Start from template</h2>
          <div class="template-list">${templates.map(templateForm).join("")}</div>
          <h2>Custom job</h2>` : ""}
        <form class="newjob" id="newjob">
          <label>name <input name="name" required placeholder="nightly-patrol"></label>
          <label>engine <select name="engine"><option>offline</option><option>ti</option></select></label>
          <label>cron (optional) <input name="cron" placeholder="0 2 * * *"></label>
          <label>interval seconds (optional) <input name="interval_seconds" type="number" min="10" placeholder="3600"></label>
          <label>budget USD <input name="budget_usd" type="number" step="0.01" value="5.0"></label>
          <label>timeout s <input name="timeout_s" type="number" value="1800"></label>
          <label>quality gate (0–1, optional) <input name="score_threshold" type="number" step="0.05" min="0" max="1" placeholder="0.7"></label>
          <label>on low score <select name="on_low_score"><option>alert</option><option>pause</option></select></label>
          <label class="checkrow"><input name="approval_required" type="checkbox"> approval required</label>
          <button class="primary submit" type="submit">Create job</button>
        </form>
      </div>
    </details>`;

  document.getElementById("newjob").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = { name: f.get("name"), engine: f.get("engine"), payload: {} };
    if (f.get("cron")) body.cron = f.get("cron");
    if (f.get("interval_seconds")) body.interval_seconds = Number(f.get("interval_seconds"));
    body.budget_usd = Number(f.get("budget_usd"));
    body.timeout_s = Number(f.get("timeout_s"));
    if (f.get("score_threshold")) body.score_threshold = Number(f.get("score_threshold"));
    body.on_low_score = f.get("on_low_score");
    body.approval_required = f.get("approval_required") === "on";
    try { await api("/jobs", { method: "POST", body: JSON.stringify(body) }); toast("job created"); render(); }
    catch (e) { toast(e.message); }
  });

  document.querySelectorAll("form.templatejob").forEach((form) => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = new FormData(form);
      const payload = {};
      for (const [key, value] of f.entries()) {
        if (key.startsWith("payload_")) payload[key.slice(8)] = String(value || "").trim();
      }
      const body = { name: String(f.get("name") || "").trim(), payload };
      const cron = String(f.get("cron") || "").trim();
      if (cron) body.cron = cron;
      try {
        const job = await api(`/jobs/from-template/${encodeURIComponent(form.dataset.template)}`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        toast("job created");
        location.hash = `#/jobs/${job.id}`;
        render();
      } catch (e) { toast(e.message); }
    });
  });
}

async function jobDetailView(id) {
  const [job, runs, stats, lessons, modes, cases] = await Promise.all([
    api(`/jobs/${id}`),
    api(`/jobs/${id}/runs`),
    api(`/jobs/${id}/stats`),
    api(`/jobs/${id}/lessons`),
    api(`/failure-modes?job_id=${encodeURIComponent(id)}`),
    api("/eval-cases"),
  ]);
  const jobCases = cases.filter((c) => c.job_id === id);
  const promoted = new Set(jobCases.map((c) => c.source_signature).filter(Boolean));
  const rows = runs.map((r) => `
    <tr class="rowlink" data-href="#/runs/${r.id}">
      <td>${badge(r.status)}</td>
      <td class="num">${r.attempt}</td>
      <td>${esc(fmtTime(r.scheduled_at))}<br><small style="color:var(--muted)">${relTime(r.scheduled_at)}</small></td>
      <td class="num">${duration(r.started_at, r.finished_at)}</td>
      <td class="num">${fmtScore(r.score)}</td>
      <td class="num">${fmtMoney(r.cost_usd)}</td>
      <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)">
        ${esc((r.error || "").split("\n").pop() || (r.result && r.result.summary) || "")}</td>
    </tr>`).join("");

  app.innerHTML = `
    <div class="crumb"><a href="#/jobs">Jobs</a> / ${esc(job.name)}</div>
    <h1>${esc(job.name)}</h1>
    <div class="sub">${esc(job.engine)} · ${esc(scheduleText(job))} · next ${relTime(job.next_run_at)}</div>
    <div class="tiles">
      <div class="tile"><div class="k">budget / run</div><div class="v">$${job.budget_usd}</div></div>
      <div class="tile"><div class="k">timeout</div><div class="v">${job.timeout_s}<small> s</small></div></div>
      <div class="tile"><div class="k">max retries</div><div class="v">${job.max_retries}</div></div>
      <div class="tile"><div class="k">approval</div><div class="v">${job.approval_required ? "required" : "off"}</div></div>
      <div class="tile"><div class="k">runs recorded</div><div class="v">${runs.length}</div></div>
    </div>
    ${jobSettingsForm(job)}
    <h2>Lessons</h2>
    <div class="card">
      <div class="lesson-list">
        ${lessons.length ? lessons.map((l) => `
          <div class="step">
            <span class="role other">lesson</span>
            <span class="name"><strong>${esc(l.title)}</strong><br><small style="color:var(--ink-2)">${esc(l.content)}</small></span>
            <span class="meta">${relTime(l.updated_at)}</span>
            <button data-dellesson="${esc(l.id)}" data-job="${esc(job.id)}">Delete</button>
          </div>`).join("") : `<div class="empty">No lessons yet — add one after you fix a recurring issue.</div>`}
      </div>
      <form class="lessonform" id="lessonform">
        <label>title <input name="title" required maxlength="200" placeholder="manual:retry-policy"></label>
        <label>content <textarea name="content" required maxlength="5000" rows="3" placeholder="What should this job remember next time?"></textarea></label>
        <button class="primary submit" type="submit">Save lesson</button>
      </form>
    </div>
    <h2>Failure modes</h2>
    <div class="card">
      ${modes.length ? `<table>
        <thead><tr><th>Signature</th><th>Error</th><th class="num">Count</th><th>Last seen</th><th></th></tr></thead>
        <tbody>${modes.map((m) => `
          <tr>
            <td><code style="font-size:12px">${esc(m.signature)}</code></td>
            <td style="max-width:420px">${esc(m.summary)}
              ${m.latest_run_id ? `<br><a href="#/runs/${m.latest_run_id}"><small>latest run</small></a>` : ""}</td>
            <td class="num">${m.count}</td>
            <td>${relTime(m.last_seen)}</td>
            <td class="actions">${promoted.has(m.signature)
              ? '<span style="color:var(--good-text);font-size:13px">✓ eval case</span>'
              : `<button class="primary" data-promote="${esc(m.signature)}" data-job="${esc(job.id)}">Promote</button>`}</td>
          </tr>`).join("")}</tbody></table>`
        : `<div class="empty">No failed runs for this job yet.</div>`}
    </div>
    <h2>Regression eval cases</h2>
    <div class="card">
      ${jobCases.length ? `<table>
        <thead><tr><th>Name</th><th>Engine</th><th class="num">Min score</th><th>Source</th><th></th></tr></thead>
        <tbody>${jobCases.map((c) => `
          <tr>
            <td><strong>${esc(c.name)}</strong>${c.enabled ? "" : ' <small style="color:var(--muted)">(disabled)</small>'}</td>
            <td>${esc(c.engine)}</td>
            <td class="num">${fmtScore(c.min_score)}</td>
            <td>${c.source_signature ? `<code style="font-size:12px">${esc(c.source_signature)}</code>` : '<span style="color:var(--muted)">manual</span>'}</td>
            <td class="actions">
              <button data-togglecase="${c.id}" data-enabled="${c.enabled ? "false" : "true"}">${c.enabled ? "Disable" : "Enable"}</button>
              <button data-delcase="${c.id}">Delete</button>
            </td>
          </tr>`).join("")}</tbody></table>`
        : `<div class="empty">No regression eval cases for this job yet.</div>`}
    </div>
    <h2>Quality score per run (last ${stats.length})</h2>
    <div class="card">${sparkline(stats, {
      value: (p) => p.score, format: fmtScore, label: "quality score per run",
      max: 1, threshold: job.score_threshold,
    })}</div>
    <h2>Cost per run (last ${stats.length})</h2>
    <div class="card">${sparkline(stats, {
      value: (p) => p.cost_usd, format: fmtMoney, label: "cost per run",
    })}</div>
    <h2>Steps per run (last ${stats.length})</h2>
    <div class="card">${sparkline(stats, {
      value: (p) => p.steps, format: (v) => `${Math.round(v)}`, label: "steps per run",
    })}</div>
    <h2>Run history</h2>
    <div class="card">
      ${runs.length ? `<table>
        <thead><tr><th>Status</th><th class="num">Attempt</th><th>Scheduled</th><th class="num">Duration</th><th class="num">Score</th><th class="num">Cost</th><th>Note</th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">No runs yet — trigger one from the jobs list.</div>`}
    </div>
    <div class="actions">
      <button class="primary" data-act="trigger" data-id="${job.id}">Run now</button>
      <button data-act="${job.paused ? "resume" : "pause"}" data-id="${job.id}">${job.paused ? "Resume" : "Pause"}</button>
    </div>`;

  document.getElementById("jobsettings").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const optionalText = (name) => {
      const value = String(f.get(name) || "").trim();
      return value ? value : null;
    };
    const optionalNumber = (name) => {
      const value = String(f.get(name) || "").trim();
      return value ? Number(value) : null;
    };
    const body = {
      name: String(f.get("name") || "").trim(),
      cron: optionalText("cron"),
      interval_seconds: optionalNumber("interval_seconds"),
      budget_usd: Number(f.get("budget_usd")),
      timeout_s: Number(f.get("timeout_s")),
      max_retries: Number(f.get("max_retries")),
      retry_backoff_s: Number(f.get("retry_backoff_s")),
      score_threshold: optionalNumber("score_threshold"),
      on_low_score: f.get("on_low_score"),
      webhook_url: optionalText("webhook_url"),
      approval_required: f.get("approval_required") === "on",
    };
    try {
      await api(`/jobs/${id}`, { method: "PATCH", body: JSON.stringify(body) });
      toast("job updated");
      render();
    } catch (e) { toast(e.message); }
  });

  document.getElementById("lessonform").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = {
      title: String(f.get("title") || "").trim(),
      content: String(f.get("content") || "").trim(),
    };
    try {
      await api(`/jobs/${id}/lessons`, { method: "POST", body: JSON.stringify(body) });
      toast("lesson saved");
      render();
    } catch (e) { toast(e.message); }
  });
}

function stepRow(s) {
  const roleClass = ["pm", "engineer", "qa", "team"].includes(s.role) ? s.role : "other";
  const io = (s.input || s.output)
    ? `<details><summary>i/o</summary><pre>${esc(JSON.stringify({ input: s.input, output: s.output }, null, 2))}</pre></details>`
    : "";
  return `
      <div class="step">
        <span class="role ${roleClass}">${esc(s.role)}</span>
        <span class="name">${esc(s.name)} ${io}</span>
        <span class="meta">${duration(s.started_at, s.finished_at)} · ${fmtMoney(s.cost_usd)} · ${(s.tokens_in || 0) + (s.tokens_out || 0)} tok</span>
      </div>`;
}

async function runDetailView(id) {
  const run = await api(`/runs/${id}`);
  const lessons = await api(`/jobs/${run.job_id}/lessons`).catch(() => []);
  const steps = run.steps.map(stepRow).join("");
  const canRerun = TERMINAL_RUN_STATUSES.has(run.status);
  const canCancel = !canRerun && run.status !== "awaiting_approval";
  const approvalActions = run.status === "awaiting_approval" ? `
    <button class="primary" data-runact="approve" data-id="${run.id}">Approve</button>
    <button data-runact="reject" data-id="${run.id}">Reject</button>` : "";
  const cancelAction = canCancel
    ? `<button data-runact="cancel" data-id="${run.id}" ${run.cancel_requested ? "disabled" : ""}>${run.cancel_requested ? "Cancel requested" : "Cancel"}</button>`
    : "";
  const rerunAction = canRerun
    ? `<button class="primary" data-runact="rerun" data-id="${run.id}">Rerun</button>`
    : "";

  app.innerHTML = `
    <div class="crumb"><a href="#/jobs">Jobs</a> / <a href="#/jobs/${run.job_id}">job</a> / run ${esc(run.id.slice(0, 8))}</div>
    <h1>Run ${esc(run.id.slice(0, 8))} ${badge(run.status)}</h1>
    <div class="sub">attempt ${run.attempt} · scheduled ${esc(fmtTime(run.scheduled_at))}</div>
    <div class="tiles">
      <div class="tile"><div class="k">quality score</div><div class="v">${fmtScore(run.score)}</div></div>
      <div class="tile"><div class="k">duration</div><div class="v">${duration(run.started_at, run.finished_at)}</div></div>
      <div class="tile"><div class="k">cost</div><div class="v">${fmtMoney(run.cost_usd)}</div></div>
      <div class="tile"><div class="k">tokens in / out</div><div class="v">${run.tokens_in}<small> / ${run.tokens_out}</small></div></div>
      <div class="tile"><div class="k">steps</div><div class="v">${run.steps.length}</div></div>
    </div>
    ${run.scores && run.scores.length ? `<h2>Scorers</h2><div class="card">${run.scores.map((s) => `
      <div class="scorecard">
        <span class="verdict ${s.passed ? "pass" : "fail"}">${s.passed ? "✓" : "✕"}</span>
        <span class="sname">${esc(s.scorer)}</span>
        <span class="bar"><i style="width:${Math.round(s.score * 100)}%"></i></span>
        <span class="sval">${fmtScore(s.score)}</span>
        ${s.detail ? `<details><summary>detail</summary><pre>${esc(JSON.stringify(s.detail, null, 2))}</pre></details>` : ""}
      </div>`).join("")}</div>` : ""}
    ${run.result ? `<h2>Result</h2><div class="card">${esc(run.result.summary || JSON.stringify(run.result))}
      ${run.result.lessons_applied ? `<br><small style="color:var(--good-text)">✓ lessons applied: ${esc(run.result.lessons_applied.join(", "))}</small>` : ""}</div>` : ""}
    ${lessons.length ? `<h2>Lessons this job knows</h2><div class="card">${lessons.map((l) => `
      <div class="step">
        <span class="role other">lesson</span>
        <span class="name"><strong>${esc(l.title)}</strong><br><small style="color:var(--ink-2)">${esc(l.content)}</small></span>
        <span class="meta">${relTime(l.updated_at)}</span>
      </div>`).join("")}</div>` : ""}
    ${run.error ? `<h2>Error</h2><div class="error-box">${esc(run.error)}</div>` : ""}
    <h2>Trace</h2>
    <div class="card" id="trace">${steps || '<div class="empty">no steps recorded yet</div>'}</div>
    ${approvalActions || cancelAction || rerunAction ? `<div class="actions">${approvalActions}${cancelAction}${rerunAction}</div>` : ""}`;

  // Live trace: stream new steps over SSE while the run is in flight, so the
  // workshop grows before your eyes. Falls back to polling if SSE fails
  // (e.g. hosted mode, where EventSource can't send the API key).
  const inFlight = run.status === "running" || run.status === "queued";
  if (inFlight && window.EventSource) {
    const trace = document.getElementById("trace");
    const seen = new Set(run.steps.map((s) => s.index));
    const es = new EventSource(`/runs/${id}/events`);
    es.addEventListener("step", (ev) => {
      const s = JSON.parse(ev.data);
      if (seen.has(s.index)) return;
      seen.add(s.index);
      const empty = trace.querySelector(".empty");
      if (empty) empty.remove();
      trace.insertAdjacentHTML("beforeend", stepRow(s));
    });
    es.addEventListener("done", () => { es.close(); render(); });  // refresh scores/result/badge
    es.onerror = () => { es.close(); schedulePoll(2000); };
  } else if (inFlight) {
    schedulePoll(2000);
  }
}

async function approvalsView() {
  const runs = await api("/approvals");
  const rows = runs.map((r) => `
    <tr>
      <td>${badge(r.status)}</td>
      <td><a href="#/runs/${r.id}">${esc(r.id.slice(0, 8))}</a><br><small style="color:var(--muted)">job ${esc(r.job_id.slice(0, 8))}</small></td>
      <td>${esc(fmtTime(r.scheduled_at))}<br><small style="color:var(--muted)">${relTime(r.scheduled_at)}</small></td>
      <td class="num">${r.attempt}</td>
      <td class="actions">
        <button class="primary" data-runact="approve" data-id="${r.id}">Approve</button>
        <button data-runact="reject" data-id="${r.id}">Reject</button>
      </td>
    </tr>`).join("");

  app.innerHTML = `
    <h1>Approvals</h1>
    <div class="sub">runs waiting for a human before the engine starts</div>
    <div class="card">
      ${runs.length ? `<table>
        <thead><tr><th>Status</th><th>Run</th><th>Requested</th><th class="num">Attempt</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">No runs are waiting for approval.</div>`}
    </div>`;
}

async function alertsView(filter = "all") {
  filter = ["open", "acked"].includes(filter) ? filter : "all";
  const filterQuery = filter === "open"
    ? "&acknowledged=false"
    : filter === "acked"
      ? "&acknowledged=true"
      : "";
  const [alerts, openAlerts] = await Promise.all([
    api(`/alerts?limit=100${filterQuery}`),
    api("/alerts?acknowledged=false&limit=1"),
  ]);
  const ALERT_BADGE = {
    auto_paused: ["paused", "‖ auto-paused"],
    run_failed: ["failed", "✕ run failed"],
    low_score: ["timed_out", "▽ low score"],
  };
  const rows = alerts.map((a) => {
    const [cls, label] = ALERT_BADGE[a.kind] || ["cancelled", a.kind];
    return `
    <tr>
      <td><span class="badge ${cls}"><span class="dot"></span>${esc(label)}</span></td>
      <td>${esc(a.message)}
          ${a.run_id ? `<br><a href="#/runs/${a.run_id}"><small>view run</small></a>` : ""}</td>
      <td>${esc(fmtTime(a.created_at))}<br><small style="color:var(--muted)">${relTime(a.created_at)}</small></td>
      <td class="actions">${a.acknowledged
        ? '<span style="color:var(--muted);font-size:13px">acked</span>'
        : `<button data-ack="${a.id}">Ack</button>`}</td>
    </tr>`;
  }).join("");

  app.innerHTML = `
    <h1>Alerts</h1>
    <div class="sub">what the quality gate and retry-exhaustion caught while nobody was watching</div>
    <div class="tabs" aria-label="Alert filters">
      <a class="${filter === "all" ? "active" : ""}" href="#/alerts">All</a>
      <a class="${filter === "open" ? "active" : ""}" href="#/alerts/open">Open</a>
      <a class="${filter === "acked" ? "active" : ""}" href="#/alerts/acked">Acked</a>
    </div>
    ${filter !== "acked" && openAlerts.length ? `<div style="margin-bottom:12px"><button data-ack-all>Ack all open</button></div>` : ""}
    <div class="card">
      ${alerts.length ? `<table>
        <thead><tr><th>Kind</th><th>Message</th><th>When</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">No ${filter === "all" ? "" : `${filter} `}alerts.</div>`}
    </div>`;
}

async function failuresView() {
  const [modes, cases, jobs] = await Promise.all([
    api("/failure-modes"),
    api("/eval-cases"),
    api("/jobs").catch(() => []),
  ]);
  const promoted = new Set(cases.map((c) => c.source_signature).filter(Boolean));
  const jobOptions = jobs.map((j) => `<option value="${esc(j.id)}">${esc(j.name)}</option>`).join("");

  const modeRows = modes.map((m) => `
    <tr>
      <td><code style="font-size:12px">${esc(m.signature)}</code></td>
      <td style="max-width:420px">${esc(m.summary)}
        ${m.latest_run_id ? `<br><a href="#/runs/${m.latest_run_id}"><small>latest run</small></a>` : ""}</td>
      <td class="num">${m.count}</td>
      <td>${relTime(m.last_seen)}</td>
      <td class="actions">${promoted.has(m.signature)
        ? '<span style="color:var(--good-text);font-size:13px">✓ eval case</span>'
        : `<button class="primary" data-promote="${esc(m.signature)}">Promote to eval case</button>`}</td>
    </tr>`).join("");

  const caseRows = cases.map((c) => `
    <tr>
      <td><strong>${esc(c.name)}</strong>${c.enabled ? "" : ' <small style="color:var(--muted)">(disabled)</small>'}</td>
      <td>${esc(c.engine)}</td>
      <td class="num">${fmtScore(c.min_score)}</td>
      <td>${c.source_signature ? `<code style="font-size:12px">${esc(c.source_signature)}</code>` : '<span style="color:var(--muted)">manual</span>'}</td>
      <td class="actions">
        <button data-togglecase="${c.id}" data-enabled="${c.enabled ? "false" : "true"}">${c.enabled ? "Disable" : "Enable"}</button>
        <button data-delcase="${c.id}">Delete</button>
      </td>
    </tr>`).join("");

  app.innerHTML = `
    <h1>Failure modes</h1>
    <div class="sub">failed runs clustered by error signature — promote recurring ones into regression eval cases</div>
    <div class="card">
      ${modes.length ? `<table>
        <thead><tr><th>Signature</th><th>Error</th><th class="num">Count</th><th>Last seen</th><th></th></tr></thead>
        <tbody>${modeRows}</tbody></table>`
      : `<div class="empty">No failures recorded — nothing to cluster.</div>`}
    </div>
    <h2>Eval cases</h2>
    <div class="sub">replayed by <code>python -m ticloud.eval.cli run</code> — wire into CI to block regressions</div>
    <details class="panel">
      <summary>＋ New eval case</summary>
      <div class="card">
        <form class="evalcase" id="evalcase">
          <label>name <input name="name" required placeholder="checkout-regression"></label>
          <label>engine <select name="engine"><option>offline</option><option>ti</option></select></label>
          <label>min score <input name="min_score" type="number" step="0.05" min="0" max="1" value="0.9"></label>
          <label>job <select name="job_id"><option value="">global</option>${jobOptions}</select></label>
          <label class="wide">payload JSON <textarea name="payload" spellcheck="false">{}</textarea></label>
          <button class="primary submit" type="submit">Create eval case</button>
        </form>
      </div>
    </details>
    <div class="card">
      ${cases.length ? `<table>
        <thead><tr><th>Name</th><th>Engine</th><th class="num">Min score</th><th>Source</th><th></th></tr></thead>
        <tbody>${caseRows}</tbody></table>`
      : `<div class="empty">No eval cases yet — promote a failure mode above.</div>`}
    </div>`;

  document.getElementById("evalcase").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    let payload = {};
    try {
      payload = JSON.parse(String(f.get("payload") || "{}").trim() || "{}");
    } catch {
      toast("payload must be valid JSON");
      return;
    }
    const body = {
      name: String(f.get("name") || "").trim(),
      engine: f.get("engine"),
      min_score: Number(f.get("min_score")),
      payload,
    };
    const jobId = String(f.get("job_id") || "").trim();
    if (jobId) body.job_id = jobId;
    try {
      await api("/eval-cases", { method: "POST", body: JSON.stringify(body) });
      toast("eval case created");
      render();
    } catch (e) { toast(e.message); }
  });
}

async function refreshAlertCount() {
  try {
    const summary = await api("/alerts/summary");
    const el = document.getElementById("alert-count");
    el.textContent = summary.unacknowledged;
    el.hidden = summary.unacknowledged === 0;
  } catch { /* topbar badge is best-effort */ }
}

async function usageView() {
  const u = await api("/usage");
  const months = u.months || [];
  const spent = u.current_month_cost_usd ?? 0;
  const budget = u.monthly_budget_usd;
  const pct = budget ? Math.min(100, Math.round((spent / budget) * 100)) : null;

  const banner = u.over_budget
    ? `<div class="card" style="border-color:var(--danger,#c0392b)"><strong>⚠ Over budget</strong> —
       new runs are blocked until next month or the cap is raised.</div>`
    : "";

  const cap = budget != null
    ? `<div class="card"><div class="sub">This month</div>
        <div style="font-size:22px;font-weight:600">${fmtMoney(spent)} <span style="color:var(--muted);font-size:15px">/ ${fmtMoney(budget)} cap (${pct}%)</span></div>
        <div style="height:8px;background:var(--line,#eee);border-radius:4px;overflow:hidden;margin-top:8px">
          <div style="height:100%;width:${pct}%;background:${u.over_budget ? "var(--danger,#c0392b)" : "var(--series-1,#3b82f6)"}"></div>
        </div></div>`
    : `<div class="card"><div class="sub">This month</div>
        <div style="font-size:22px;font-weight:600">${fmtMoney(spent)}</div>
        <div style="color:var(--muted);font-size:13px;margin-top:4px">No spend cap set.</div></div>`;

  const rows = months.slice().reverse().map((m) => `
    <tr><td>${esc(m.month)}</td><td>${m.runs}</td><td>${m.succeeded}</td>
        <td>${fmtMoney(m.cost_usd)}</td><td>${m.tokens_in} / ${m.tokens_out}</td></tr>`).join("");

  app.innerHTML = `
    <h1>Usage${u.tenant_id ? "" : " (self-host)"}</h1>
    <div class="sub">monthly run spend from the platform's own accounting — judge spend excluded</div>
    ${banner}
    ${cap}
    <div class="card">
      ${months.length ? `<table>
        <thead><tr><th>Month</th><th>Runs</th><th>Succeeded</th><th>Cost</th><th>Tokens in/out</th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">No usage yet.</div>`}
    </div>`;
}

/* ---------- router & polling ---------- */

function schedulePoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => {
    if (document.activeElement && document.activeElement.closest("form")) schedulePoll(ms);
    else if (!document.hidden) render();
    else schedulePoll(ms);
  }, ms);
}

async function render() {
  clearTimeout(pollTimer);
  const hash = location.hash || "#/jobs";
  const [, view, id] = hash.split("/");
  refreshAlertCount();
  try {
    if (view === "runs" && id) await runDetailView(id);
    else if (view === "usage") { await usageView(); schedulePoll(15000); }
    else if (view === "approvals") { await approvalsView(); schedulePoll(5000); }
    else if (view === "failures") { await failuresView(); schedulePoll(6000); }
    else if (view === "alerts") { await alertsView(id || "all"); schedulePoll(5000); }
    else if (view === "jobs" && id) { await jobDetailView(id); schedulePoll(3000); }
    else { await jobsView(); schedulePoll(3000); }
  } catch (e) {
    app.innerHTML = `<div class="card"><div class="empty">⚠ ${esc(e.message)}</div></div>`;
    schedulePoll(4000);
  }
}

app.addEventListener("click", (ev) => {
  const ackBtn = ev.target.closest("button[data-ack]");
  if (ackBtn) {
    ev.stopPropagation();
    act("POST", `/alerts/${ackBtn.dataset.ack}/ack`, render);
    return;
  }
  if (ev.target.closest("button[data-ack-all]")) {
    ev.stopPropagation();
    act("POST", "/alerts/ack-all", render);
    return;
  }
  const promoteBtn = ev.target.closest("button[data-promote]");
  if (promoteBtn) {
    ev.stopPropagation();
    const body = { signature: promoteBtn.dataset.promote };
    if (promoteBtn.dataset.job) body.job_id = promoteBtn.dataset.job;
    api("/failure-modes/promote", {
      method: "POST",
      body: JSON.stringify(body),
    }).then(() => { toast("eval case created"); render(); }).catch((e) => toast(e.message));
    return;
  }
  const delBtn = ev.target.closest("button[data-delcase]");
  if (delBtn) {
    ev.stopPropagation();
    act("DELETE", `/eval-cases/${delBtn.dataset.delcase}`, render);
    return;
  }
  const toggleBtn = ev.target.closest("button[data-togglecase]");
  if (toggleBtn) {
    ev.stopPropagation();
    api(`/eval-cases/${toggleBtn.dataset.togglecase}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: toggleBtn.dataset.enabled === "true" }),
    }).then(() => { toast("eval case updated"); render(); }).catch((e) => toast(e.message));
    return;
  }
  const lessonBtn = ev.target.closest("button[data-dellesson]");
  if (lessonBtn) {
    ev.stopPropagation();
    act("DELETE", `/jobs/${lessonBtn.dataset.job}/lessons/${lessonBtn.dataset.dellesson}`, render);
    return;
  }
  const btn = ev.target.closest("button[data-act]");
  if (btn) {
    ev.stopPropagation();
    const { act: action, id } = btn.dataset;
    const method = "POST";
    act(method, `/jobs/${id}/${action}`, render);
    return;
  }
  const runBtn = ev.target.closest("button[data-runact]");
  if (runBtn) {
    ev.stopPropagation();
    if (runBtn.dataset.runact === "rerun") {
      api(`/runs/${runBtn.dataset.id}/rerun`, { method: "POST" })
        .then((run) => { toast("run queued"); location.hash = `#/runs/${run.id}`; render(); })
        .catch((e) => toast(e.message));
      return;
    }
    act("POST", `/runs/${runBtn.dataset.id}/${runBtn.dataset.runact}`, render);
    return;
  }
  const row = ev.target.closest("tr.rowlink");
  if (row && !ev.target.closest("[data-noclick]")) location.hash = row.dataset.href;
});

window.addEventListener("hashchange", render);
document.addEventListener("visibilitychange", () => { if (!document.hidden) render(); });
render();
