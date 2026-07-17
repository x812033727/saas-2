# Launch Notes（發佈素材）

口號：**"An AI dev team on cron, with quality gates."**

---

## Show HN 草稿

**標題（≤80 字元，擇一）**
1. `Show HN: Ti Cloud – Cron for AI agents, with quality gates that auto-pause bad runs`
2. `Show HN: Run AI agents on a schedule; every run is scored, drift gets caught`
3. `Show HN: My AI agents kept silently breaking overnight, so I built this`

**貼文**

> Unattended agents (nightly repo patrols, dependency upgrades, CI
> babysitters) have a structural problem: nobody watches each run. A chat
> agent that breaks gets caught in seconds; a cron agent that breaks fails
> silently for weeks.
>
> Schedulers (Temporal/Inngest/cron) run things reliably but don't
> understand agents. LLM observability (Langfuse/LangSmith) shows traces
> but doesn't own the schedule and won't stop a degraded agent from
> running again tomorrow.
>
> Ti Cloud composes the two, with a loop in the middle:
>
> - agent-native scheduling: cron/interval + per-run cost budgets,
>   timeouts, and retries that carry the previous error as context
> - every finished run is scored (completion, trajectory health — stuck
>   loops, review verdicts — cost anomaly vs the job's own history,
>   optional LLM judge); below threshold → alert → auto-pause
> - failures cluster into failure modes; one click turns a recurring one
>   into a regression eval case; `ticloud.eval.cli run` replays the set
>   and exits non-zero in CI until the failure mode is actually fixed
> - failures also become per-job lessons that the next run reads — the
>   demo's flaky job fails once, records the lesson, and the retry
>   succeeds because of it
>
> Everything runs without API keys (offline demo engine included):
> `docker compose up` + `python -m ticloud.demo` + open /ui/.
> Apache-2.0, Python + FastAPI + no-build frontend. Self-hosting is
> zero-dependency by design (deterministic failure clustering, judge
> optional); a hosted version will fund development.
>
> Would love feedback on the gate semantics — what would make you trust
> an agent enough to let it auto-pause (or keep running) unattended?

**發佈時間建議**：週二–週四，台灣時間 21:00–23:00（美東早上）。

---

## 定位 FAQ（留言區備彈）

**vs Temporal / Inngest / cron？**
它們是通用工作流引擎——可靠地執行任何東西，但不知道「執行的東西是 agent」。
沒有 token/成本預算、不評軌跡品質、沒有「這次輸出比上週爛」的概念。
Ti Cloud 可以與它們並存：你甚至能用 Temporal 觸發、Ti Cloud 評分。

**vs Langfuse / LangSmith / Braintrust？**
觀測與評測平台給你 trace 和 eval 跑分，但不擁有排程——出問題時它們讓你「看見」，
不會替你「擋下」。Ti Cloud 的差異是閉環：score → gate → auto-pause →
failure → eval case → CI red until fixed。

**vs Devin / Copilot Workspace？**
它們是「指派一個任務給 AI」——單次、互動式。Ti Cloud 是「讓 AI 團隊值班」——
排程制、無人值守、品質閘門把關、知識累積。互補而非替代。

**為什麼聚類不用 embedding？**
Self-host 零依賴是刻意的：決定性簽名（去噪後雜湊）就能抓住絕大多數重複故障，
而且離線可重現。語意聚類是雲端版的加值，不是開源版的門檻。

**LLM judge 用什麼模型？**
預設 `claude-opus-4-8`，per-job 可換。judge 花費記在評分明細、不混進 agent
成本——否則會汙染 drift 訊號。沒有 key 就自動跳過，規則式評分仍是保底。

**真的能跑我的 agent 嗎？**
`AgentEngine` protocol 三個接點：跑（`run(ctx)`）、記步驟（`ctx.record_step`）、
記帳（`ctx.add_cost`）。旗艦引擎（Ti 多專家工作坊）已接入——`engine: "ti"`
＋`TICLOUD_TI_PATH` 指向 Ti checkout 即可排程真實多專家工作坊；offline 引擎
就是 protocol 的參考實作。

---

## 90 秒 demo 腳本

1.（0–15s）`docker compose up` + `python -m ticloud.demo`，開 /ui/。
   「三個排程中的 agent job，全部零 API key。」
2.（15–40s）點 `dep-upgrade`：score sparkline 跌破虛線 gate → 排程顯示
   paused → Alerts 頁三種告警。「它連續失敗，平台自己評分、告警、停跑——
   沒有人盯著。」
3.（40–65s）點 `nightly-patrol` 最新 run：succeeded, attempt 2,
   「✓ lessons applied」→ 往下捲到 Lessons 卡。「第一次踩坑失敗，教訓入庫，
   重試讀到教訓就過了。用越久越聰明。」
4.（65–90s）Failures 頁：故障聚類 → Promote to eval case →
   終端跑 `python -m ticloud.eval.cli run` 出現紅色 FAIL exit 1。
   「線上故障一鍵變回歸測試，修好之前 CI 一直是紅的。」

---

## 發佈檢查清單

- [ ] repo 改名/轉移到正式名稱（目前 saas-2）＋ About 描述與 topics
- [ ] main 分支合併、tag v0.1.0
- [ ] GitHub Discussions 開啟（waitlist / feedback 用）
- [ ] README 頂部加 badge（CI、license）
- [ ] 錄 demo GIF（照 90 秒腳本，工具：vhs 或 LICEcap）放 README 頂部
- [ ] Show HN 發文 + 同步 X/Reddit r/LocalLLaMA（改寫成社群語氣）
- [ ] 準備好前 24 小時回留言（定位 FAQ 就是彈藥庫）
