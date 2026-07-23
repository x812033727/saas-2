# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足，已留待後續改良：

- [x] 靜態巡檢候選 B：webhook URL 已補 http/https 驗證與測試
- [x] 靜態巡檢候選 A：Anthropic judge 已補 `messages.parse` / `beta.messages.parse` 相容 fallback 與測試
- [ ] 尚未由本環境開 PR；目前修改留在工作樹，交由流程後續提交
