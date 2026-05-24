# Adoption Note

## 接入日期

2026-05-24

## 接入来源

已有项目。

## 接入方式

基于当前目录进行轻量观察，只创建本地 `project_ledger/`。

## 已观察到的内容

- README 显示项目名为 `AI Chat Archive`。
- 项目是本地 AI 对话归档系统，使用 FastAPI、SQLite FTS5 和 Jinja2。
- 支持导入 OpenAI、Gemini、Grok 和 DeepSeek 的官方导出文件。
- 主要入口文件是 `aiarchive/main.py`。
- 依赖记录在 `requirements.txt`。
- README 给出的运行命令是 `uvicorn aiarchive.main:app --host 0.0.0.0 --port 8000`。
- 当前目录是 Git 仓库，最近 commit 为 `e0b2686 Add ChatGPT import support and fix conversation layout`。
- 接入时 worktree 有未提交内容，包含既有的 `aiarchive/services.py` 改动。

## 不确定内容

- 项目所有者对 Project ID 的最终命名偏好待确认。
- 当前功能完整度和发布状态待确认。
- 测试命令未在轻量观察中确认。
- 部署方式除本地运行外待确认。

## 接入原则

- 本账本由已有项目接入生成，不代表所有推测都已经被用户确认。
- 不修改业务代码。
- 不移动文件。
- 不重构目录。
- 不安装依赖。
- 不运行构建或测试。
- 不把推测写成事实。

## 下一步建议

请用户确认项目目标、当前状态、运行方式和 Project ID 是否采用 `aiarchive`。
