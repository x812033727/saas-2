# Task #2 靜態巡檢候選缺陷清單

> 巡檢範圍：api/、eval/、config.py、web/
> 禁區：scheduler/worker.py、/ready 路由、auth_mode（#27 已修）
> 方法：純靜態閱讀，無執行

---

## 候選 B — 高置信度（已修）

**`platform/ticloud/api/schemas.py:21`**（同樣出現在 :61、:275）

| 欄位 | 內容 |
|------|------|
| 缺陷 | `webhook_url: str \| None = Field(default=None, max_length=500)` 只限長度，無 URL scheme 驗證 |
| 觸發條件 | POST /jobs 帶 `webhook_url="ftp://x"` 或 `"not-a-url"` → 201 成功，但 notify.py:62 的 `except Exception` 靜默吞掉 delivery 錯誤，使用者毫無回饋 |
| 建議修法 | 改用 `pydantic.AnyHttpUrl`，或仿同檔 :26 的 `_valid_action` 範式補 `@field_validator` 驗 `http/https` scheme |
| 影響面 | 所有含 webhook 的 job；影響面廣、修法自我包含 |

**處理狀態**：已在 API schema 與全域 `TICLOUD_WEBHOOK_URL` 設定共用 `validate_webhook_url`，只允許 `http/https` 且拒絕空 host/空白字元；已補 API 與 config 測試。

---

## 候選 A — 中置信度（已修）

**`platform/ticloud/eval/judge.py:64`**

| 欄位 | 內容 |
|------|------|
| 缺陷 | `client.messages.parse(...)` 非 Anthropic SDK 穩定路徑（應為 `client.beta.messages.parse()`） |
| 觸發條件 | `job.scorers` 含 `{"judge": {"enabled": true}}` 時每次評分觸發，可能拋 `AttributeError`，由 base.py:56 的 `except` 接住後 score=0.0，judge gate 靜默失效 |
| 待確認 | `client.messages.parse` 是否為當前安裝 SDK 版本的合法 API（靜態無法自證），**請在執行環境確認 anthropic 套件版本再決定是否修** |
| 影響面 | judge scorer 啟用時評分全部記 0，屬「假工作」缺陷，不炸服務但結果無意義 |

**處理狀態**：已在本環境確認 `anthropic 0.119.0` 同時提供 `messages.parse` 與 `beta.messages.parse`；judge 目前優先用穩定 `messages.parse`，缺少時 fallback 到 `beta.messages.parse`，並以假 SDK 測試覆蓋。

---

## 排除項（有防護，不納入候選）

- `eval/failures.py:39` — 已有 `lines[-1] if lines else ""` 防護
- `config.py` — pydantic_settings 原生安全轉型，int/bool 轉型有保護
- `web/index.html` — `#/failures`、`#/usage` 路由均在 app.js:433 有處理

---

## 流程備注

- `#3 -> #2` 依賴已移除：#3（PR #27）基於 #1 候選已完成，#2 降為非阻塞 backlog。
- 候選 A/B 均已落地；下一輪不要再從本清單重複挑選。
