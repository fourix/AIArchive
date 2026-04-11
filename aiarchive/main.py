from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .config import settings
from .db import get_connection, initialize_database
from .services import (
    get_conversation_detail,
    get_platform_browse_location,
    import_file,
    import_gemini_takeout_zip,
    list_conversations,
    list_platform_overview,
    list_recent_imports,
    purge_platform_data,
    supported_platforms,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.imports_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    initialize_database()
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
templates = Jinja2Templates(directory=settings.templates_dir)


def _to_system_local(value: str) -> datetime | None:
    if not value:
        return None

    normalized = value.strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone()


def format_datetime(value: str) -> str:
    localized = _to_system_local(value)
    if localized is None:
        return ""
    return localized.strftime("%Y-%m-%d %H:%M")


def format_time(value: str) -> str:
    localized = _to_system_local(value)
    if localized is None:
        return ""
    return localized.strftime("%H:%M")


def format_month_day_time(value: str) -> str:
    localized = _to_system_local(value)
    if localized is None:
        return ""
    return localized.strftime("%m-%d %H:%M")


def _highlight_terms(query: str) -> list[str]:
    normalized = (query or "").strip()
    if not normalized:
        return []

    if any("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in normalized):
        return [normalized]

    parts = [part for part in re.split(r"\s+", normalized) if part]
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(part)
    unique.sort(key=len, reverse=True)
    return unique


def highlight_query(value: str, query: str) -> Markup:
    text = value or ""
    escaped_text = escape(text)
    terms = _highlight_terms(query)
    if not terms:
        return Markup(escaped_text)

    pattern = "|".join(re.escape(term) for term in terms)
    highlighted = re.sub(
        f"({pattern})",
        r"<mark>\1</mark>",
        str(escaped_text),
        flags=re.IGNORECASE,
    )
    return Markup(highlighted)


templates.env.filters["datetime_display"] = format_datetime
templates.env.filters["time_display"] = format_time
templates.env.filters["month_day_time_display"] = format_month_day_time
templates.env.filters["highlight_query"] = highlight_query


def render_message_content(value: str) -> Markup:
    text = value or ""
    if not text.strip():
        return Markup("")

    code_block_pattern = re.compile(r"```([A-Za-z0-9_-]+)?\s*\r?\n(.*?)```", re.DOTALL)

    def render_inline(inline_text: str) -> str:
        escaped = str(escape(inline_text))
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", escaped)
        return escaped

    def render_text_block(text_block: str) -> list[str]:
        stripped = text_block.strip()
        if not stripped:
            return []

        lines = stripped.splitlines()
        blocks: list[str] = []
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph:
                return
            joined = "<br>".join(render_inline(line.strip()) for line in paragraph)
            blocks.append(f"<p>{joined}</p>")
            paragraph.clear()

        for raw_line in lines:
            line = raw_line.rstrip()
            plain = line.strip()

            if not plain:
                flush_paragraph()
                continue

            if plain == "---":
                flush_paragraph()
                blocks.append("<hr>")
                continue

            heading = re.match(r"^(#{1,4})\s+(.*)$", plain)
            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                blocks.append(f"<h{level}>{render_inline(heading.group(2).strip())}</h{level}>")
                continue

            paragraph.append(line)

        flush_paragraph()
        return blocks

    parts: list[str] = []
    last_end = 0
    for match in code_block_pattern.finditer(text):
        if match.start() > last_end:
            parts.extend(render_text_block(text[last_end:match.start()]))

        language = (match.group(1) or "").strip().lower()
        code = match.group(2).strip()
        if language == "mermaid":
            parts.append(f'<div class="mermaid">{escape(code)}</div>')
        else:
            escaped_code = escape(code)
            language_class = f' class="language-{language}"' if language else ""
            parts.append(f"<pre><code{language_class}>{escaped_code}</code></pre>")
        last_end = match.end()

    if last_end < len(text):
        parts.extend(render_text_block(text[last_end:]))

    if not parts:
        parts.extend(render_text_block(text))

    return Markup("".join(parts))


def render_plain_text(value: str) -> Markup:
    text = value or ""
    if not text:
        return Markup("")
    return Markup(f"<pre>{escape(text)}</pre>")


templates.env.filters["render_message_content"] = render_message_content
templates.env.filters["render_plain_text"] = render_plain_text


def _is_zip_upload(filename: str, raw: bytes) -> bool:
    if raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06") or raw.startswith(b"PK\x07\x08"):
        return True
    return (filename or "").lower().endswith(".zip")


def platform_ui_context(platform: str) -> dict[str, str]:
    normalized = platform.lower()
    if normalized == "gemini":
        return {
            "collection_label": "记录",
            "item_label": "记录",
            "items_label": "记录",
            "note": "Gemini 导出以独立活动记录存储，不是线程式会话结构。",
        }
    return {
        "collection_label": "会话",
        "item_label": "会话",
        "items_label": "消息",
        "note": "",
    }


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    imported: str = "",
    purged: str = "",
):
    with get_connection() as connection:
        imports = list_recent_imports(connection)
        platform_overview = list_platform_overview(connection)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "imports": imports,
            "platform_overview": platform_overview,
            "platforms": supported_platforms(),
            "filters": {
                "imported": imported,
                "purged": purged,
            },
            "nav_current": "home",
        },
    )


@app.get("/import", response_class=HTMLResponse)
def import_page(
    request: Request,
    imported: str = "",
    purged: str = "",
    platform: str = "",
):
    with get_connection() as connection:
        imports = list_recent_imports(connection)

    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "request": request,
            "imports": imports,
            "platforms": supported_platforms(),
            "filters": {
                "imported": imported,
                "purged": purged,
                "platform": platform,
            },
            "nav_current": "import",
        },
    )


@app.get("/browse", response_class=HTMLResponse)
def browse_page(
    request: Request,
    purged: str = "",
    platform: str = "",
):
    with get_connection() as connection:
        platform_overview = list_platform_overview(connection)

    return templates.TemplateResponse(
        request,
        "browse.html",
        {
            "request": request,
            "platform_overview": platform_overview,
            "filters": {
                "purged": purged,
                "platform": platform,
            },
            "nav_current": "browse",
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    platform: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    has_search_input = any(value.strip() for value in (q, platform, date_from, date_to))
    if has_search_input:
        with get_connection() as connection:
            conversations_page = list_conversations(
                connection,
                query=q,
                platform=platform,
                date_from=date_from,
                date_to=date_to,
                page=page,
            )
    else:
        conversations_page = SimpleNamespace(
            items=[],
            page=1,
            total_pages=1,
            has_prev=False,
            has_next=False,
            total=0,
            page_size=100,
        )

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "request": request,
            "conversations": conversations_page.items,
            "pagination": {
                "page": conversations_page.page,
                "total_pages": conversations_page.total_pages,
                "has_prev": conversations_page.has_prev,
                "has_next": conversations_page.has_next,
                "total": conversations_page.total,
                "page_size": conversations_page.page_size,
            },
            "platforms": supported_platforms(),
            "filters": {
                "q": q,
                "platform": platform,
                "date_from": date_from,
                "date_to": date_to,
                "page": page,
                "has_search_input": has_search_input,
            },
            "nav_current": "search",
        },
    )


@app.post("/import")
async def import_json(
    platform: str = Form(...),
    file: UploadFile = File(...),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    normalized_platform = platform.lower()
    filename = file.filename or "upload"

    with get_connection() as connection:
        try:
            if normalized_platform == "gemini":
                if not _is_zip_upload(filename, raw):
                    raise ValueError("Gemini import requires the original Google Takeout ZIP file")
                import_gemini_takeout_zip(
                    connection,
                    filename=filename,
                    raw=raw,
                )
            else:
                import_file(
                    connection,
                    platform=normalized_platform,
                    filename=filename,
                    raw=raw,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/import?imported=1", status_code=303)


@app.post("/platforms/{platform}/purge")
def purge_platform(platform: str):
    normalized_platform = platform.lower()
    with get_connection() as connection:
        try:
            purge_platform_data(connection, normalized_platform)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=f"/browse?purged=1&platform={normalized_platform}", status_code=303)


@app.post("/api/platforms/{platform}/purge")
def purge_platform_api(platform: str):
    normalized_platform = platform.lower()
    with get_connection() as connection:
        try:
            result = purge_platform_data(connection, normalized_platform)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "platform": result.platform,
        "conversations_deleted": result.conversations_deleted,
        "messages_deleted": result.messages_deleted,
        "imports_deleted": result.imports_deleted,
    }


@app.get("/api/conversations")
def conversations_api(
    q: str = "",
    platform: str = "",
    date_from: str = "",
    date_to: str = "",
):
    with get_connection() as connection:
        items_page = list_conversations(
            connection,
            query=q,
            platform=platform,
            date_from=date_from,
            date_to=date_to,
        )
    return {
        "items": items_page.items,
        "page": items_page.page,
        "total_pages": items_page.total_pages,
        "total": items_page.total,
    }


@app.get("/platforms/{platform}", response_class=HTMLResponse)
def platform_browse(
    request: Request,
    platform: str,
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    normalized_platform = platform.lower()
    if normalized_platform not in supported_platforms():
        raise HTTPException(status_code=404, detail="Platform not found")

    with get_connection() as connection:
        conversations_page = list_conversations(
            connection,
            platform=normalized_platform,
            date_from=date_from,
            date_to=date_to,
            page=page,
        )

    return templates.TemplateResponse(
        request,
        "platform.html",
        {
            "request": request,
            "platform": normalized_platform,
            "conversations": conversations_page.items,
            "pagination": {
                "page": conversations_page.page,
                "total_pages": conversations_page.total_pages,
                "has_prev": conversations_page.has_prev,
                "has_next": conversations_page.has_next,
                "total": conversations_page.total,
                "page_size": conversations_page.page_size,
            },
            "ui": platform_ui_context(normalized_platform),
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
                "page": page,
            },
            "nav_current": "browse",
        },
    )


@app.get("/api/platforms/{platform}/conversations")
def platform_browse_api(
    platform: str,
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
):
    normalized_platform = platform.lower()
    if normalized_platform not in supported_platforms():
        raise HTTPException(status_code=404, detail="Platform not found")

    with get_connection() as connection:
        items_page = list_conversations(
            connection,
            platform=normalized_platform,
            date_from=date_from,
            date_to=date_to,
            page=page,
        )
    return {
        "platform": normalized_platform,
        "items": items_page.items,
        "page": items_page.page,
        "total_pages": items_page.total_pages,
        "total": items_page.total,
    }


@app.get("/conversations/{conversation_id}", response_class=HTMLResponse)
def conversation_detail(request: Request, conversation_id: int):
    with get_connection() as connection:
        payload = get_conversation_detail(connection, conversation_id)
        browse_location = get_platform_browse_location(connection, conversation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return templates.TemplateResponse(
        request,
        "conversation.html",
        {
            "request": request,
            "conversation": payload["conversation"],
            "messages": payload["messages"],
            "browse_location": browse_location,
            "ui": platform_ui_context(str(payload["conversation"]["platform"])),
            "nav_current": "",
        },
    )


@app.get("/api/conversations/{conversation_id}")
def conversation_detail_api(conversation_id: int):
    with get_connection() as connection:
        payload = get_conversation_detail(connection, conversation_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return payload


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe() -> Response:
    return Response(status_code=204)
