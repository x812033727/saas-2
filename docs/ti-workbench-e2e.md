# Ti Cloud 工作台 E2E 驗收說明

## 功能：手機版設定頁

本文件驗證 Ti Cloud 工作台支援手機版設定頁：Personal Workspace 的設定頁
可在行動裝置瀏覽器上完整操作下列設定：

- **任務預設值**：最大成本上限（美元）、最長執行時間
- **外觀與版面**：主題切換（跟隨系統 / 淺色 / 深色）、預設顯示 Agent 活動
- **鍵盤快捷鍵**：檢視常用快捷鍵對照表

設定偏好僅存在本機瀏覽器（localStorage），不上傳至 Ti Worker，
符合最小資料收集原則。

## 三項安全原則

1. **只建立 Draft PR**：所有變更必須以 Draft（草稿）狀態發 PR，
   確保人類審核後才能 merge，不得繞過 review 流程。

2. **隔離 branch**：每個功能或修復必須在獨立 feature branch 上開發，
   禁止直接 push 至 `main`，避免未完成的變更污染主線。

3. **附件不得進入 commit**：截圖、視覺參考圖等附件（`.ticloud-attachments/`）
   僅作為設計參考，不得 `git add` 或提交進版本庫。

## 相關測試

最小相關測試：`platform/tests/test_ui.py`（4 項，全數通過）

```
tests/test_ui.py::test_root_redirects_to_ui        PASSED
tests/test_ui.py::test_ui_serves_dashboard          PASSED
tests/test_ui.py::test_overview_includes_last_run   PASSED
tests/test_ui.py::test_job_stats_series             PASSED
```

執行指令：
```bash
python3 -m venv .venv
.venv/bin/pip install -e "./platform[dev]"
.venv/bin/python -m pytest platform/tests/test_ui.py -v
```
