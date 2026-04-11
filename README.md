# AI Chat Archive

A local AI chat archive system built with FastAPI, SQLite FTS5, and Jinja2. It imports exports from OpenAI, Gemini, Grok, and DeepSeek into a unified schema for search and browsing on small hardware, including Raspberry Pi.

## Features

- Unified schema for conversations and messages
- Incremental import with duplicate protection
- Full-text search with SQLite FTS5
- Filter by platform and message date
- Browse conversations and inspect messages in order
- Lightweight server-rendered UI

## Project Structure

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
```

## Database Schema

Core tables:

- `conversations`: normalized conversation metadata
- `messages`: normalized messages, timestamp-first, linked to conversations
- `imports`: import history for operational visibility
- `message_fts`: FTS5 table for searching message content

Important uniqueness rules:

- `conversations(platform, source_conversation_id)` is unique
- `messages(conversation_id, source_message_id)` is unique
- `messages(message_hash)` is unique

These constraints allow repeat imports without creating duplicates.

## Run

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the server:

```bash
uvicorn aiarchive.main:app --host 0.0.0.0 --port 8000
```

4. Open `http://localhost:8000`.

## Importing Data

Use the web UI on the import page, or submit ZIP exports to the import endpoint:

```bash
curl -X POST http://localhost:8000/import \
  -F "platform=openai" \
  -F "file=@/path/to/export.zip"
```

Supported platform keys:

- `openai`
- `gemini`
- `grok`
- `deepseek`

OpenAI import format:

- Upload the ZIP export directly.

Gemini import format:

- Upload the original Google Takeout ZIP directly.
- The ZIP must contain the standard Gemini Apps activity JSON from Google Takeout.
- Any sibling media or attachment files in that same Gemini Apps folder are imported automatically.
- Assistant HTML is preserved for display, and referenced assets are copied into app-managed storage under `data/media/`.

DeepSeek import format:

- Upload the DeepSeek ZIP export directly.
- The archive root must contain `conversations.json`.
- `user.json` is ignored during import.

Grok import format:

- Upload the ZIP export directly.
- For ZIP imports, the app recursively finds `prod-grok-backend.json`.
- Sibling attachments are read from the `prod-mc-asset-server` directory next to that JSON file.
- Each attachment is stored using its attachment id, and the app infers the file extension from the `content` file header.

## FastAPI Endpoints

- `GET /` HTML search and browse UI
- `POST /import` multipart import for ZIP exports
- `GET /conversations/{conversation_id}` HTML conversation detail
- `GET /api/conversations` JSON conversation list with search and filters
- `GET /api/conversations/{conversation_id}` JSON conversation detail

## Raspberry Pi Notes

- Python 3.11+ recommended
- SQLite FTS5 is included in standard Python builds on Raspberry Pi OS
- The app keeps dependencies minimal and uses synchronous SQLite access to reduce operational complexity
