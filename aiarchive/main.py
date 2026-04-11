from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import get_connection, initialize_database
from .services import (
    extract_uploaded_zip,
    get_conversation_detail,
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


templates.env.filters["datetime_display"] = format_datetime
templates.env.filters["time_display"] = format_time
templates.env.filters["month_day_time_display"] = format_month_day_time


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
    extracted: str = "",
    extract_path: str = "",
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
                "extracted": extracted,
                "extract_path": extract_path,
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
    with get_connection() as connection:
        conversations_page = list_conversations(
            connection,
            query=q,
            platform=platform,
            date_from=date_from,
            date_to=date_to,
            page=page,
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


@app.post("/import/debug-zip")
async def import_debug_zip(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    filename = file.filename or "upload.zip"
    if not _is_zip_upload(filename, raw):
        raise HTTPException(status_code=400, detail="Please upload a ZIP file")

    try:
        extract_root = extract_uploaded_zip(filename=filename, raw=raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    relative_path = extract_root.relative_to(settings.base_dir).as_posix()
    return RedirectResponse(
        url=f"/import?extracted=1&extract_path={quote(relative_path, safe='')}",
        status_code=303,
    )


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
    if payload is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return templates.TemplateResponse(
        request,
        "conversation.html",
        {
            "request": request,
            "conversation": payload["conversation"],
            "messages": payload["messages"],
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
