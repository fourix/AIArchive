from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlencode
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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


def ensure_runtime_directories() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.imports_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_runtime_directories()
    initialize_database()
    yield


ensure_runtime_directories()
app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
templates = Jinja2Templates(directory=settings.templates_dir)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if wants_json(request):
        language = resolve_language(request)
        detail = exc.detail if isinstance(exc.detail, str) else make_translator(language)("error_default_message")
        return JSONResponse(status_code=exc.status_code, content={"detail": localize_error_message(language, detail)})

    language = resolve_language(request)
    translate = make_translator(language)
    detail = exc.detail if isinstance(exc.detail, str) else translate("error_default_message")
    message = localize_error_message(language, detail)
    response = template_response(
        request,
        "error.html",
        {
            "status_code": exc.status_code,
            "error_heading": translate(error_heading_key(exc.status_code)),
            "error_message": message,
            "nav_current": "",
        },
    )
    response.status_code = exc.status_code
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if wants_json(request):
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    language = resolve_language(request)
    translate = make_translator(language)
    response = template_response(
        request,
        "error.html",
        {
            "status_code": 500,
            "error_heading": translate("error_heading_500"),
            "error_message": translate("error_default_message"),
            "nav_current": "",
        },
    )
    response.status_code = 500
    return response

DEFAULT_LANGUAGE = "en"
FLASH_IMPORT_ERROR_COOKIE = "aiarchive_import_error"
SUPPORTED_LANGUAGES = {
    "en": "English",
    "zh": "中文",
}
LANGUAGE_ATTR = {
    "en": "en",
    "zh": "zh-CN",
}
TRANSLATIONS = {
    "en": {
        "site_title": "AI Chat Archive",
        "tagline": "A local, searchable archive for multi-platform AI conversation history.",
        "nav_import": "Import",
        "nav_browse": "Browse",
        "nav_search": "Search",
        "language_label": "Language",
        "home": "Home",
        "import_page_title": "Import",
        "import_heading": "Import Export Files",
        "import_intro": "Upload ZIP exports from supported platforms and merge them into your local archive. Gemini uses the original Google Takeout ZIP, DeepSeek reads conversations.json from the archive root, and Grok automatically finds prod-grok-backend.json and its sibling attachments.",
        "platform_label": "Platform",
        "export_file_label": "Export File",
        "choose_file": "Choose File",
        "no_file_chosen": "No file chosen",
        "start_import": "Start Import",
        "import_complete": "Import complete.",
        "import_failed": "Import failed: {message}",
        "recent_imports": "Recent Imports",
        "no_import_records": "No imports yet.",
        "hero_title": "AI Chat Archive",
        "hero_intro": "Archive official exports from OpenAI, Gemini, Grok, and DeepSeek into a lightweight FastAPI and SQLite system with full-text search.",
        "go_import": "Go to Import",
        "go_browse": "Go to Browse",
        "go_search": "Go to Search",
        "platform_overview": "Platform Overview",
        "latest_activity": "Latest activity {value}",
        "no_imported_conversations": "No imported conversations yet",
        "browse_title": "Browse",
        "browse_heading": "Browse by Platform",
        "browse_intro": "Open a platform to browse all archived conversations, or clear one platform during development.",
        "purged_notice": "Cleared all archived data for {platform}.",
        "clear_platform_data": "Clear {platform} Data",
        "clear_platform_confirm": "Clear all archived data for {platform}?",
        "search_title": "Search",
        "search_heading": "Search Conversations",
        "search_intro": "Search across imported message text and filter by platform and date.",
        "keyword_label": "Keyword",
        "search_placeholder": "Search message text",
        "all_platforms": "All",
        "start_date_label": "Start Date",
        "end_date_label": "End Date",
        "apply_filters": "Apply Filters",
        "search_results": "Search Results",
        "page_summary": "Page {page} of {total_pages}. {total} total.",
        "page_fraction": "Page {page} / {total_pages}",
        "prev_page": "Previous",
        "next_page": "Next",
        "back_to_top": "Back to Top",
        "no_search_results": "No conversations matched the current filters.",
        "search_empty_prompt": "Enter a keyword or choose filters to start searching.",
        "browse_platform": "Browse {platform}",
        "back_to_home": "Back to archive home",
        "records_label": "Records",
        "record_label": "record",
        "conversations_label": "conversations",
        "conversation_label": "conversation",
        "messages_label": "messages",
        "message_label": "message",
        "gemini_note": "Gemini exports are stored as standalone activity records rather than threaded conversations.",
        "record_time": "Recorded {value}",
        "first_message": "First message {value}",
        "no_platform_items": "No {item_label} found for this platform and date range.",
        "conversation_updated": "Updated {value}",
        "back_to_platform_list": "Back to {platform} list",
        "back_to_platform_hint": "Jump to page {page} and the current conversation",
        "conversation_empty_gemini": "This Gemini record has no message body stored. Re-import the Gemini export with the current importer.",
        "conversation_empty_default": "This conversation has no saved messages yet.",
        "error_page_title": "Error",
        "error_heading_400": "Request Error",
        "error_heading_403": "Forbidden",
        "error_heading_404": "Not Found",
        "error_heading_500": "Something Went Wrong",
        "error_default_message": "The request could not be completed.",
        "error_back_home": "Back to Home",
        "error_platform_not_found": "Platform not found",
        "error_conversation_not_found": "Conversation not found",
        "error_uploaded_file_empty": "Uploaded file is empty",
        "error_invalid_zip": "Uploaded file is not a valid ZIP archive",
        "error_gemini_requires_takeout": "Gemini import requires the original Google Takeout ZIP file",
        "error_gemini_not_export": "This ZIP does not look like a Gemini Takeout export",
        "error_deepseek_not_export": "This ZIP does not look like a DeepSeek export",
        "error_grok_not_export": "This ZIP does not look like a Grok export",
        "error_zip_platform_mismatch": "This ZIP does not match the selected platform",
        "error_gemini_missing_json": "Gemini Takeout ZIP is missing the expected Gemini Apps activity JSON file",
        "error_deepseek_missing_json": "DeepSeek ZIP is missing conversations.json at the archive root",
        "error_grok_missing_json": "Grok ZIP is missing prod-grok-backend.json",
    },
    "zh": {
        "site_title": "AI 对话档案",
        "tagline": "本地保存、可检索的多平台 AI 对话历史。",
        "nav_import": "导入",
        "nav_browse": "浏览",
        "nav_search": "搜索",
        "language_label": "语言",
        "home": "首页",
        "import_page_title": "导入",
        "import_heading": "导入导出文件",
        "import_intro": "上传各平台导出的 ZIP 文件并合并到本地档案。Gemini 使用原始 Google Takeout ZIP，DeepSeek 从压缩包根目录读取 conversations.json，Grok 会自动定位 prod-grok-backend.json 及同级附件。",
        "platform_label": "平台",
        "export_file_label": "导出文件",
        "start_import": "开始导入",
        "import_complete": "导入完成。",
        "recent_imports": "最近导入",
        "no_import_records": "还没有导入记录。",
        "hero_title": "AI 对话档案",
        "hero_intro": "使用轻量的 FastAPI 和 SQLite，把 OpenAI、Gemini、Grok、DeepSeek 的官方导出归档到本地，并支持全文检索。",
        "go_import": "前往导入",
        "go_browse": "前往浏览",
        "go_search": "前往搜索",
        "platform_overview": "平台概览",
        "latest_activity": "最近活动 {value}",
        "no_imported_conversations": "还没有导入会话",
        "browse_title": "浏览",
        "browse_heading": "按平台浏览",
        "browse_intro": "打开某个平台查看全部归档会话，也可以在开发阶段清空单个平台的数据。",
        "purged_notice": "已清空 {platform} 的全部归档数据。",
        "clear_platform_data": "清空 {platform} 数据",
        "clear_platform_confirm": "确认清空 {platform} 的全部归档数据？",
        "search_title": "搜索",
        "search_heading": "搜索会话",
        "search_intro": "搜索所有已导入平台中的消息文本，并按平台与日期筛选。",
        "keyword_label": "关键词",
        "search_placeholder": "搜索消息文本",
        "all_platforms": "全部",
        "start_date_label": "开始日期",
        "end_date_label": "结束日期",
        "apply_filters": "应用筛选",
        "search_results": "搜索结果",
        "page_summary": "第 {page} 页，共 {total_pages} 页，总计 {total} 条。",
        "page_fraction": "第 {page} / {total_pages} 页",
        "prev_page": "上一页",
        "next_page": "下一页",
        "back_to_top": "回到顶部",
        "no_search_results": "当前筛选条件下没有匹配的会话。",
        "search_empty_prompt": "请输入关键词或选择筛选条件后再开始搜索。",
        "browse_platform": "浏览 {platform}",
        "back_to_home": "返回档案首页",
        "records_label": "记录",
        "record_label": "记录",
        "conversations_label": "会话",
        "conversation_label": "会话",
        "messages_label": "条消息",
        "message_label": "消息",
        "gemini_note": "Gemini 导出以独立活动记录存储，不是线程式会话结构。",
        "record_time": "记录时间 {value}",
        "first_message": "首条消息 {value}",
        "no_platform_items": "当前平台与日期范围下没有可显示的{item_label}。",
        "conversation_updated": "更新于 {value}",
        "back_to_platform_list": "返回 {platform} 列表",
        "back_to_platform_hint": "定位到第 {page} 页当前会话位置",
        "conversation_empty_gemini": "这条 Gemini 记录导入时没有填充消息正文。请使用当前版本导入器重新导入 Gemini 数据。",
        "conversation_empty_default": "这个会话暂时没有保存任何消息。",
    },
}

TRANSLATIONS["zh"].update(
    {
        "error_page_title": "错误",
        "error_heading_400": "请求错误",
        "error_heading_403": "禁止访问",
        "error_heading_404": "页面不存在",
        "error_heading_500": "发生错误",
        "error_default_message": "请求未能完成。",
        "error_back_home": "返回首页",
        "error_platform_not_found": "平台不存在",
        "error_conversation_not_found": "会话不存在",
        "error_uploaded_file_empty": "上传文件为空",
        "error_invalid_zip": "上传的文件不是有效的 ZIP 压缩包",
        "error_gemini_requires_takeout": "Gemini 导入需要原始 Google Takeout ZIP 文件",
        "error_gemini_not_export": "这个 ZIP 看起来不像 Gemini Takeout 导出文件",
        "error_deepseek_not_export": "这个 ZIP 看起来不像 DeepSeek 导出文件",
        "error_grok_not_export": "这个 ZIP 看起来不像 Grok 导出文件",
        "error_zip_platform_mismatch": "这个 ZIP 与当前选择的平台不匹配",
        "error_gemini_missing_json": "Gemini Takeout ZIP 中缺少预期的 Gemini Apps 活动 JSON 文件",
        "error_deepseek_missing_json": "DeepSeek ZIP 根目录缺少 conversations.json",
        "error_grok_missing_json": "Grok ZIP 中缺少 prod-grok-backend.json",
    }
)

TRANSLATIONS["zh"]["choose_file"] = "选择文件"
TRANSLATIONS["zh"]["no_file_chosen"] = "未选择文件"
TRANSLATIONS["en"].update(
    {
        "open_settings": "Open Settings",
        "settings_title": "Settings",
        "settings_close": "Close Settings",
        "settings_language": "Language",
        "settings_theme": "Theme",
        "theme_light": "Light",
        "theme_dark": "Dark",
    }
)
TRANSLATIONS["zh"].update(
    {
        "open_settings": "打开设置",
        "settings_title": "设置",
        "settings_close": "关闭设置",
        "settings_language": "语言",
        "settings_theme": "主题",
        "theme_light": "亮色",
        "theme_dark": "暗色",
    }
)


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


def resolve_language(request: Request) -> str:
    query_lang = request.query_params.get("lang", "").strip().lower()
    if query_lang in SUPPORTED_LANGUAGES:
        return query_lang

    cookie_lang = request.cookies.get("lang", "").strip().lower()
    if cookie_lang in SUPPORTED_LANGUAGES:
        return cookie_lang

    return DEFAULT_LANGUAGE


def make_translator(language: str):
    catalog = TRANSLATIONS.get(language, TRANSLATIONS[DEFAULT_LANGUAGE])

    def translate(key: str, **kwargs: object) -> str:
        text = catalog.get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key) or key
        return text.format(**kwargs) if kwargs else text

    return translate


def build_language_switch_urls(request: Request) -> dict[str, str]:
    return {code: str(request.url.include_query_params(lang=code)) for code in SUPPORTED_LANGUAGES}


def template_response(
    request: Request,
    template_name: str,
    context: dict[str, object],
) -> HTMLResponse:
    language = resolve_language(request)
    merged_context = {
        **context,
        "request": request,
        "lang": language,
        "lang_attr": LANGUAGE_ATTR.get(language, LANGUAGE_ATTR[DEFAULT_LANGUAGE]),
        "t": make_translator(language),
        "languages": SUPPORTED_LANGUAGES,
        "language_switch_urls": build_language_switch_urls(request),
    }
    response = templates.TemplateResponse(request, template_name, merged_context)
    if request.query_params.get("lang", "").strip().lower() in SUPPORTED_LANGUAGES:
        response.set_cookie("lang", language, max_age=31536000, samesite="lax")
    return response


def wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def error_heading_key(status_code: int) -> str:
    return {
        400: "error_heading_400",
        403: "error_heading_403",
        404: "error_heading_404",
        500: "error_heading_500",
    }.get(status_code, "error_page_title")


def localize_error_message(language: str, message: str) -> str:
    if not message:
        return make_translator(language)("error_default_message")

    translate = make_translator(language)
    mapping = {
        "Platform not found": "error_platform_not_found",
        "Conversation not found": "error_conversation_not_found",
        "Uploaded file is empty": "error_uploaded_file_empty",
        "Uploaded file is not a valid ZIP archive": "error_invalid_zip",
        "Gemini import requires the original Google Takeout ZIP file": "error_gemini_requires_takeout",
        "This ZIP does not look like a Gemini Takeout export": "error_gemini_not_export",
        "This ZIP does not look like a DeepSeek export": "error_deepseek_not_export",
        "This ZIP does not look like a Grok export": "error_grok_not_export",
        "This ZIP does not match the selected platform": "error_zip_platform_mismatch",
        "Gemini Takeout ZIP is missing the expected Gemini Apps activity JSON file": "error_gemini_missing_json",
        "DeepSeek ZIP is missing conversations.json at the archive root": "error_deepseek_missing_json",
        "Grok ZIP is missing prod-grok-backend.json": "error_grok_missing_json",
    }
    key = mapping.get(message)
    return translate(key) if key else message


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

    return template_response(
        request,
        "index.html",
        {
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

    flash_error = unquote(request.cookies.get(FLASH_IMPORT_ERROR_COOKIE, ""))
    response = template_response(
        request,
        "import.html",
        {
            "imports": imports,
            "platforms": supported_platforms(),
            "filters": {
                "imported": imported,
                "error": flash_error,
                "purged": purged,
                "platform": platform,
            },
            "nav_current": "import",
        },
    )
    if flash_error:
        response.delete_cookie(FLASH_IMPORT_ERROR_COOKIE)
    return response


@app.get("/browse", response_class=HTMLResponse)
def browse_page(
    request: Request,
    purged: str = "",
    platform: str = "",
):
    with get_connection() as connection:
        platform_overview = list_platform_overview(connection)

    return template_response(
        request,
        "browse.html",
        {
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

    return template_response(
        request,
        "search.html",
        {
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
    request: Request,
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
            current_lang = resolve_language(request)
            query = {"platform": normalized_platform}
            if current_lang != DEFAULT_LANGUAGE:
                query["lang"] = current_lang
            response = RedirectResponse(url=f"/import?{urlencode(query)}", status_code=303)
            response.set_cookie(
                FLASH_IMPORT_ERROR_COOKIE,
                quote(localize_error_message(current_lang, str(exc)), safe=""),
                max_age=60,
                samesite="lax",
            )
            return response

    query = {"imported": "1"}
    current_lang = resolve_language(request)
    if current_lang != DEFAULT_LANGUAGE:
        query["lang"] = current_lang
    return RedirectResponse(url=f"/import?{urlencode(query)}", status_code=303)


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

    return template_response(
        request,
        "platform.html",
        {
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
    return template_response(
        request,
        "conversation.html",
        {
            "conversation": payload["conversation"],
            "messages": payload["messages"],
            "browse_location": browse_location,
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
