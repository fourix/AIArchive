# AI Chat Archive

一个基于 FastAPI、SQLite FTS5 和 Jinja2 的本地 AI 对话归档系统。项目主要面向个人使用：导入你自己从 OpenAI、Gemini、Grok 和 DeepSeek 获取的官方导出文件，统一归档后更方便地检索和回看过去的聊天内容，也适合运行在 Raspberry Pi 这类小型设备上。

## 说明

- 本项目是一个独立的个人归档工具，仅用于检索你自己的导出聊天记录，与 OpenAI、Google、xAI、DeepSeek 没有官方关联，也未获得其背书或赞助。
- 只应导入和使用你有合法权利访问、保存和处理的数据与附件。
- 本项目面向用户自行提供的官方导出文件，不应用于抓取、绕过平台限制，或以违反平台条款、第三方权利的方式批量提取数据。
- 你需要自行确保导入内容、附件、导出文件、截图或再分发的数据不侵犯版权、隐私、合同义务或其他权利。
- 除非你拥有合法权利，否则不要公开发布 `data/` 目录、数据库文件、导入的附件或真实聊天导出内容。

## 功能特性

- 使用统一 schema 存储会话与消息
- 支持增量导入与重复数据去重
- 基于 SQLite FTS5 的全文检索
- 支持按平台和消息日期筛选
- 支持浏览会话并按顺序查看消息
- 轻量的服务端渲染 UI

## 项目结构

```text
aiarchive/
  __init__.py
  config.py
  db.py
  main.py
  models.py
  services.py
  importers/
    __init__.py
    base.py
    common.py
    deepseek.py
    gemini.py
    grok.py
    openai.py
  templates/
    base.html
    index.html
    conversation.html
  static/
    style.css
data/
imports/
requirements.txt
README.md
README.zh-CN.md
```

## 数据库结构

核心表：

- `conversations`：标准化后的会话元数据
- `messages`：标准化后的消息数据，以时间戳为主要排序依据，并关联到会话
- `imports`：导入历史，便于查看导入情况
- `message_fts`：用于消息内容全文检索的 FTS5 虚表

关键唯一性约束：

- `conversations(platform, source_conversation_id)` 唯一
- `messages(conversation_id, source_message_id)` 唯一
- `messages(message_hash)` 唯一

这些约束保证重复导入时不会产生重复数据。

## 运行方式

1. 创建并激活虚拟环境。
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 启动服务：

```bash
uvicorn aiarchive.main:app --host 0.0.0.0 --port 8000
```

4. 打开 `http://localhost:8000`。

## 导入数据

可以通过网页导入页面上传，也可以调用导入接口提交 ZIP 导出文件：

```bash
curl -X POST http://localhost:8000/import \
  -F "platform=openai" \
  -F "file=@/path/to/export.zip"
```

支持的平台键值：

- `openai`
- `gemini`
- `grok`
- `deepseek`

OpenAI 导入格式：

- 直接上传 OpenAI 官方导出的 ZIP 文件。

Gemini 导入格式：

- 直接上传原始 Google Takeout ZIP。
- ZIP 内必须包含 Google Takeout 标准的 Gemini Apps 活动 JSON。
- 同一 Gemini Apps 目录下的媒体与附件会自动一起导入。
- Assistant 的 HTML 内容会保留用于展示，引用到的资源会复制到应用管理的 `data/media/` 目录中。

DeepSeek 导入格式：

- 直接上传 DeepSeek ZIP 导出文件。
- 压缩包根目录必须包含 `conversations.json`。
- `user.json` 会在导入时被忽略。

Grok 导入格式：

- 直接上传 ZIP 导出文件。
- 导入时会递归查找 `prod-grok-backend.json`。
- 其同级目录下 `prod-mc-asset-server` 中的附件也会一并读取。
- 每个附件会按其附件 id 存储，文件扩展名根据 `content` 文件头自动推断。

## FastAPI 端点

- `GET /`：HTML 界面首页，包含搜索和浏览入口
- `POST /import`：通过 multipart 上传 ZIP 导出文件并导入
- `GET /conversations/{conversation_id}`：HTML 会话详情页
- `GET /api/conversations`：带搜索和筛选功能的 JSON 会话列表
- `GET /api/conversations/{conversation_id}`：JSON 会话详情

## Raspberry Pi 说明

- 建议使用 Python 3.11+
- Raspberry Pi OS 标准 Python 构建通常已包含 SQLite FTS5
- 项目依赖较少，并使用同步 SQLite 访问以降低运维复杂度

## 许可证

本项目基于 MIT License 发布。详见 [LICENSE](LICENSE)。
