/* Ti Cloud dashboard — no build step, hash routing, polling refresh. */

const app = document.getElementById("app");
let pollTimer = null;

/* ---------- helpers ---------- */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
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
};
const badge = (status) =>
  `<span class="badge ${esc(status)}"><span class="dot"></span>${esc(STATUS_LABEL[status] || status)}</span>`;

function scheduleText(job) {
  if (job.paused) return "paused";
  if (job.cron) return `cron ${job.cron}`;
  if (job.interval_seconds) return `every ${job.interval_seconds}s`;
  return "manual only";
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

async function jobsView() {
  const jobs = await api("/overview");
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
        <form class="newjob" id="newjob">
          <label>name <input name="name" required placeholder="nightly-patrol"></label>
          <label>engine <select name="engine"><option>offline</option><option>ti</option></select></label>
          <label>cron (optional) <input name="cron" placeholder="0 2 * * *"></label>
          <label>interval seconds (optional) <input name="interval_seconds" type="number" min="10" placeholder="3600"></label>
          <label>budget USD <input name="budget_usd" type="number" step="0.01" value="5.0"></label>
          <label>timeout s <input name="timeout_s" type="number" value="1800"></label>
          <label>quality gate (0–1, optional) <input name="score_threshold" type="number" step="0.05" min="0" max="1" placeholder="0.7"></label>
          <label>on low score <select name="on_low_score"><option>alert</option><option>pause</option></select></label>
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
    try { await api("/jobs", { method: "POST", body: JSON.stringify(body) }); toast("job created"); render(); }
    catch (e) { toast(e.message); }
  });
}

async function jobDetailView(id) {
  const [job, runs, stats] = await Promise.all([
    api(`/jobs/${id}`), api(`/jobs/${id}/runs`), api(`/jobs/${id}/stats`),
  ]);
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
      <div class="tile"><div class="k">runs recorded</div><div class="v">${runs.length}</div></div>
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
}

async function runDetailView(id) {
  const run = await api(`/runs/${id}`);
  const lessons = await api(`/jobs/${run.job_id}/lessons`).catch(() => []);
  const steps = run.steps.map((s) => {
    const roleClass = ["pm", "engineer", "qa", "team"].includes(s.role) ? s.role : "other";
    const io = (s.input || s.output)
      ? `<details><summary>i/o</summary><pre>${esc(JSON.stringify({ input: s.input, output: s.output }, null, 2))}</pre></details>`
      : "";
    return `
      <div class="step">
        <span class="role ${roleClass}">${esc(s.role)}</span>
        <span class="name">${esc(s.name)} ${io}</span>
        <span class="meta">${duration(s.started_at, s.finished_at)} · ${fmtMoney(s.cost_usd)} · ${s.tokens_in + s.tokens_out} tok</span>
      </div>`;
  }).join("");

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
    <div class="card">${steps || '<div class="empty">no steps recorded yet</div>'}</div>`;

  // Live trace: keep refreshing while the run is in flight.
  if (run.status === "running" || run.status === "queued") schedulePoll(2000);
}

async function alertsView() {
  const alerts = await api("/alerts?limit=100");
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
    <div class="card">
      ${alerts.length ? `<table>
        <thead><tr><th>Kind</th><th>Message</th><th>When</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">No alerts — everything your agents shipped passed the gate.</div>`}
    </div>`;
}

async function failuresView() {
  const [modes, cases] = await Promise.all([api("/failure-modes"), api("/eval-cases")]);
  const promoted = new Set(cases.map((c) => c.source_signature).filter(Boolean));

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
      <td class="actions"><button data-delcase="${c.id}">Delete</button></td>
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
    <div class="card">
      ${cases.length ? `<table>
        <thead><tr><th>Name</th><th>Engine</th><th class="num">Min score</th><th>Source</th><th></th></tr></thead>
        <tbody>${caseRows}</tbody></table>`
      : `<div class="empty">No eval cases yet — promote a failure mode above.</div>`}
    </div>`;
}

async function refreshAlertCount() {
  try {
    const open = await api("/alerts?acknowledged=false&limit=100");
    const el = document.getElementById("alert-count");
    el.textContent = open.length;
    el.hidden = open.length === 0;
  } catch { /* topbar badge is best-effort */ }
}

/* ---------- router & polling ---------- */

function schedulePoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => { if (!document.hidden) render(); else schedulePoll(ms); }, ms);
}

async function render() {
  clearTimeout(pollTimer);
  const hash = location.hash || "#/jobs";
  const [, view, id] = hash.split("/");
  refreshAlertCount();
  try {
    if (view === "runs" && id) await runDetailView(id);
    else if (view === "failures") { await failuresView(); schedulePoll(6000); }
    else if (view === "alerts") { await alertsView(); schedulePoll(5000); }
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
  const promoteBtn = ev.target.closest("button[data-promote]");
  if (promoteBtn) {
    ev.stopPropagation();
    api("/failure-modes/promote", {
      method: "POST",
      body: JSON.stringify({ signature: promoteBtn.dataset.promote }),
    }).then(() => { toast("eval case created"); render(); }).catch((e) => toast(e.message));
    return;
  }
  const delBtn = ev.target.closest("button[data-delcase]");
  if (delBtn) {
    ev.stopPropagation();
    act("DELETE", `/eval-cases/${delBtn.dataset.delcase}`, render);
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
  const row = ev.target.closest("tr.rowlink");
  if (row && !ev.target.closest("[data-noclick]")) location.hash = row.dataset.href;
});

window.addEventListener("hashchange", render);
document.addEventListener("visibilitychange", () => { if (!document.hidden) render(); });
render();
