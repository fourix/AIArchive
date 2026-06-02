# Status

## Project State

adopted

## Current Focus

为已有项目建立最小 Project Ledger 接手入口。

## Last Meaningful Update

2026-05-26：新增 Claude 导出 ZIP 导入支持，并验证可导入用户提供的 Claude 导出样例。

## What Works Now

- README 记录了本地运行方式和导入入口。
- 项目包含 FastAPI 应用入口、导入器、模板、静态资源和 SQLite 数据目录。
- 支持 OpenAI、Gemini、Claude、Grok 和 DeepSeek 平台导入。

## What Is Broken / Unknown

- 测试命令待确认。
- 部署方式待确认。
- 接入时 worktree 有未提交内容，包含既有的 `aiarchive/services.py` 改动。

## Current Risks

- 不要把接入阶段推测当成事实。
- 不要在未确认前公开或提交真实导出数据、数据库和附件。

## Next Best Step

1. 请用户确认项目目标、状态和运行方式。
2. 请用户确认 Project ID 是否采用 `aiarchive`。

## Review Needed

- 确认 Project ID 是否采用 `aiarchive`。
- 确认项目状态。
- 确认测试命令和部署方式。

## If Resuming After Long Pause

先读 `03_PROJECT_CARD.md`、`04_STATUS.md` 和最近 run note。
