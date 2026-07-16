# SaaS 規劃：Ti Cloud —— 排程制自主 AI 開發團隊（loop/cron + 品質閘門）

## Context（為什麼做這個）

使用者是工程師、有小團隊/資金，想做可商用 SaaS。經收斂定案：
- 賽道：**LLM/Agent 開發者工具**，全球英文市場，**開源核心 + 雲端付費** GTM
- 主打：**loop/cron engineering**（無人值守、排程/迴圈執行的 agent）
- 底座：**整合使用者既有專案 Ti**（github.com/x812033727/Ti）的優點

### Ti 是什麼（既有資產）
AI 專家工作坊：PM / 工程師 / 資深工程師 / QA 多角色協作 —— 需求澄清 → 任務拆解 →
架構辯論 → 迭代執行（寫碼+測試+review，每任務最多 3 輪）→ demo + 回顧。
已有的獨特優點：
1. **真自主協作**：專家真的執行程式、跑測試、迭代改進（不只是討論）
2. **知識沉澱**：RESEARCH.md / DECISIONS.md / 跨 session 教訓庫，避免重工
3. **平行執行**：獨立任務在 git worktree 平行跑再合併
4. **GitHub 整合**：成果直接發 branch/PR，可選 auto-merge
5. **多 LLM**：Claude Agent SDK 為主，OpenAI/Gemini/MiniMax/本地模型可切換
6. **安全預設**：branch 保護、資源上限、狀態完整性驗證
7. **離線 demo 模式**：無 API key 可完整體驗（銷售/獲客利器）
8. **即時串流 Web UI**：全程直播協作過程

### 市場縫隙
- 最有價值的 agent 正從「聊天型」轉向「**無人值守型**」；這類 agent 沒人看每次執行，
  出錯默默持續。89% 團隊有 observability、僅 52% 有正經 eval，故障多在縫隙中。
- 排程工具（Airflow/Temporal/Inngest）不懂 agent；觀測工具（Langfuse/LangSmith）不管排程、
  更沒有「多專家協作 + 自動改進 repo」的能力。
- **Ti + cron + 品管的組合目前市場上沒有直接對手**：Devin/Copilot Workspace 是「單次指派任務」，
  不是「排程制、持續自主改進、品質閘門把關」的常駐團隊。

### 定位（一句話）
> **"An autonomous AI dev team on a schedule — it patrols your repos, ships quality-gated PRs, and never forgets what it learned."**
> 排程制 AI 開發團隊：每晚巡檢、多專家協作、品質不過關不出手、知識越用越累積。

---

## 產品策略：Ti 的優點 × loop/cron 的四個支柱

| 支柱 | 內容 | 來自 |
|------|------|------|
| 1. Agent-native cron/loop 引擎（主打） | cron/interval/事件觸發；token/成本預算、逾時、非確定性重試、併發控制；「loop until 條件」；human-approval 閘門（Ti 已有高風險動作的安全預設，擴成核准流） | 新建 + Ti 安全機制 |
| 2. 多專家協作執行體 | 每個排程 job 跑的不是單一 agent，而是 Ti 工作坊（PM 澄清 → 拆解 → 辯論 → 執行 → QA review）；worktree 平行執行 | **Ti 核心（studio/ 直接重用）** |
| 3. 每次執行自動品管 + drift | 執行後自動評分（規則式 + LLM-judge + 軌跡評測）；低於門檻告警/暫停排程；run-over-run 趨勢圖抓緩慢劣化。Ti 的 QA 角色升級為正式 eval gate | 新建 + Ti QA 角色 |
| 4. 知識飛輪（護城河） | Ti 的教訓庫/DECISIONS/RESEARCH 跨執行累積 → 每晚的巡檢越跑越聰明；失敗執行自動聚類成故障模式 → 進 eval-set 防重蹈覆轍。**資料累積型護城河，用越久越難換掉** | **Ti 知識沉澱 + 新 eval-mining** |

### 殺手級使用場景（旗艦模板）
- **Nightly repo patrol**：每晚掃 repo → 找 bug/改進機會 → 多專家協作修 → 發品質閘門把關的 PR → 早上人類 review merge
- **持續依賴/安全維護**：定期升級依賴、修 CVE、跑回歸
- **CI babysitter**：CI 紅了自動診斷、修復、重推
- 平台保持通用（SDK 可跑任何 agent job），但用這些模板獲客

### GTM：開源核心 + 雲端付費
- **開源**：Ti 引擎（已有）、SDK、self-host 排程器、trace UI、基本 scorer、離線 demo 模式
- **雲端付費**：托管排程與 worker（天然付費點：不用自己維運）、多 repo/團隊協作、
  告警整合、drift 分析、auto eval-mining、跨組織知識庫、SSO
- 定價：Free（self-host / 雲端 1 repo 小量）→ Team ~$59/seat → Pro/Enterprise（按執行量加購）
- **離線 demo 模式是獲客大招**：訪客不填 API key 就能看完整協作直播，轉換率利器

---

## MVP 範圍（三階段）—— 以 Ti 為底座改造，不是從零寫

### Phase 1 — Ti + 排程引擎 + 多租戶化（約 5–7 週）
- **排程引擎**：Python worker + Postgres 佇列（SKIP LOCKED），cron/interval 觸發 Ti workshop
  執行；預算/逾時強制、重試、併發控制
- **Ti 改造**：workshop 執行從「互動觸發」改為「可被排程器無頭調度」；
  執行紀錄結構化落庫（目前的日誌 → OTel GenAI 慣例 trace）
- **Web UI 擴充**：在 Ti 既有直播 UI 上加 job 列表、執行歷史時間軸、trace 樹
  （角色/工具呼叫/token/成本/延遲）
- 里程碑：設一個「每晚 2 點巡檢 repo」的 job → 自動跑 Ti workshop → 早上在 UI 看到
  完整過程與產出 PR

### Phase 2 — Eval gate + 告警 + drift（約 5–7 週）
- **Scorer 框架**：規則式（測試通過率、步數上限、diff 規模）+ LLM-judge
  （Claude opus 精準 / sonnet 省成本）+ 軌跡評測（工具選擇、迴圈偵測）；
  Ti 的 QA review 輸出納入評分
- **品質閘門**：分數低 → 不發 PR / 告警（Slack/email）/ 自動暫停排程
- **Run-over-run dashboard**：分數/成本/步數趨勢
- **Human-approval 流**：高風險動作（merge、部署）暫停等核准
- 里程碑：故意讓 job 品質劣化 → 下次執行被閘門擋下 + 收到告警

### Phase 3 — 知識飛輪 + eval-mining + CI gate（約 6–8 週）
- Ti 教訓庫/DECISIONS 升級為**多租戶知識庫**（per-repo/per-org，可檢索）
- 失敗執行 embedding 聚類 → 故障模式 → 一鍵轉 eval 案例
- CLI + GitHub Action：PR 時跑 eval-set，回歸擋 merge
- 里程碑：閉環 —— 線上故障 → 自動變考題 → 修好 → CI 綠燈 → 排程恢復，
  且下次巡檢引用教訓庫避開同類錯誤

---

## 技術架構（最大化重用 Ti）

| 層 | 建議 | 來源 |
|----|------|------|
| Agent 引擎 | Ti `studio/`（角色、工作流狀態機、worktree 平行、知識沉澱） | **重用** |
| LLM 層 | Claude Agent SDK + 多 LLM fallback | **重用（Ti 已有）** |
| 排程引擎 | Python worker + Postgres 佇列 | 新建 |
| API | FastAPI（Ti 已用）+ 管理 API + OTLP ingestion | 擴充 |
| DB | Postgres（jobs/runs/scores/知識庫）；trace 量大再上 ClickHouse | 新建 |
| 前端 | Ti `web/` 直播 UI 為基礎；dashboard 部分建議漸進導入 Next.js | 擴充 |
| Judge | Claude（實作前先讀 `claude-api` skill 確認 model id/定價/caching） | 新建 |
| 部署 | Docker Compose 一鍵 self-host（Ti `deploy/` 擴充）+ 雲端托管 | 擴充 |

### Repo 策略
- **Ti repo**：保持開源引擎定位，重構為可被調度的核心（引擎化）
- **saas-2 repo（本 repo）**：雲端平台層 —— 排程器、多租戶、計費、託管 UI、eval 服務
- monorepo 或雙 repo 皆可，建議 saas-2 以 submodule/package 依賴 Ti，界線即開源/商業界線

---

## 商業化路徑

1. **驗證（1–2 週）**：dogfooding —— 用平台自己排程「每晚巡檢 Ti 與 saas-2 repo」；
   找 3–5 個團隊試用 nightly patrol，驗證「願意讓 AI 團隊定期自主改 repo」的信任門檻與付費意願
2. **開源發佈**：Ti 引擎化 + Phase 1 完成後發 Show HN / r/LocalLLaMA / X，
   口號 "an AI dev team on cron"；離線 demo 模式讓人零門檻體驗
3. **雲端 waitlist → Team 訂閱**：托管排程 + drift + eval-mining + 知識庫為付費牆
4. **計費**：seat + 執行量（agent-run 分鐘或 token 轉嫁加成）

---

## 驗證方式

- **Phase 1**：`docker-compose up` → 建每 15 分鐘的測試 job → Ti workshop 被排程器無頭觸發
  → UI 顯示執行歷史 + trace 樹；驗證預算上限會中止、重試生效、兩個 job 併發不互踩
- **Phase 2**：設 scorer 門檻 → 注入劣化（改壞 prompt）→ 下次執行被擋、收到告警、
  排程自動暫停；drift 圖呈現轉折；human-approval 流程走通（暫停→核准→續跑）
- **Phase 3**：餵含已知故障的執行紀錄 → 聚出故障模式 → eval 案例可重現；
  GitHub Action 在 PR 上擋回歸；巡檢日誌中可見引用教訓庫的證據
- **端到端**：nightly patrol 在真 repo 跑一週 → 至少產出一個人類願意 merge 的 PR，
  且一次品質閘門成功攔截 —— 這就是可拿去賣的 demo

---

## 風險與對策
- **信任門檻（讓 AI 自主改 repo）**：預設「只發 PR 不 merge」+ human-approval +
  品質閘門；Ti 已有 branch 保護等安全預設，行銷上主打「never ships unreviewed」
- **排程可靠性是門面**：Phase 1 嚴測時鐘/重試/冪等；Postgres 佇列從簡但正確
- **大廠競爭（Devin、Copilot 等）**：差異化在「排程制常駐 + 品質閘門 + 知識飛輪」，
  不是單次任務指派；知識庫是換掉成本
- **Ti 引擎化的重構成本**：Phase 1 只做最小改造（無頭調度 + 結構化落庫），不大改架構

---

## 下一步（核准後）
1. 在 saas-2 建平台層骨架（排程器 + API + docker-compose），以依賴方式引入 Ti
2. Ti 最小引擎化改造：無頭調度介面 + 執行紀錄結構化
3. 起第一個 dogfood job：每晚巡檢兩個 repo
