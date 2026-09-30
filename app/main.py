from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .services.jm_service import ARCHIVE_ROOT, DOWNLOAD_ROOT, download_task, normalize_ids, preview, search
from .services.task_manager import TaskManager
from .config import cache_cleaner, config, config_reloader
from .database import database

app = FastAPI(title="JMComic Web Downloader", version="0.1.0")
manager = TaskManager(download_task)
INDEX = Path(__file__).parent / "web" / "index.html"
LOGIN = Path(__file__).parent / "web" / "login.html"
FAVICON = Path(__file__).parent / "web" / "favicon.ico"
ADMIN = Path(__file__).parent / "web" / "admin.html"


@app.on_event("startup")
def start_maintenance() -> None:
    manager.recover()
    cache_cleaner.start()
    config_reloader.start()


@app.on_event("shutdown")
def stop_maintenance() -> None:
    config_reloader.stop()
    cache_cleaner.stop()


class IdRequest(BaseModel):
    ids: list[str] = Field(min_length=1)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    page: int = Field(default=1, ge=1, le=9999)
    search_type: str = "site"
    order_by: str = "mr"
    time: str = "a"
    category: str = "0"


class UserCreateRequest(BaseModel):
    token: str = Field(min_length=12, max_length=256)
    note: str = Field(default="", max_length=200)
    enabled: bool = True


class UserUpdateRequest(BaseModel):
    token: str | None = Field(default=None, min_length=12, max_length=256)
    note: str = Field(default="", max_length=200)
    enabled: bool = True


def require_user(
    x_access_token: str | None = Header(default=None),
    jm_session: str | None = Cookie(default=None),
) -> int:
    token = x_access_token or jm_session
    user_id = database.authenticate(token or "")
    if user_id is None:
        raise HTTPException(status_code=401, detail="访问令牌无效")
    return user_id


def require_admin(user_id: int = Depends(require_user)) -> int:
    if not database.is_admin(user_id):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user_id


@app.post("/api/session", status_code=204)
def create_session(response: Response, x_access_token: str | None = Header(default=None)) -> None:
    user_id = database.authenticate(x_access_token or "")
    if user_id is None:
        raise HTTPException(status_code=401, detail="访问令牌无效")
    response.set_cookie(
        "jm_session", x_access_token, httponly=True, samesite="strict",
        secure=bool(config.get("auth", {}).get("secure_cookie", False)), max_age=86400,
    )


@app.delete("/api/session", status_code=204)
def delete_session(response: Response) -> None:
    response.delete_cookie("jm_session", samesite="strict")


@app.get("/", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
def login(jm_session: str | None = Cookie(default=None)) -> str:
    if database.authenticate(jm_session or "") is not None:
        return Response(status_code=307, headers={"Location": "/app"})
    return LOGIN.read_text(encoding="utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(FAVICON, media_type="image/x-icon")


@app.get("/app", response_class=HTMLResponse)
def index(
    x_access_token: str | None = Header(default=None),
    jm_session: str | None = Cookie(default=None),
) -> str:
    if database.authenticate(x_access_token or jm_session or "") is None:
        raise HTTPException(status_code=401, detail="访问令牌无效")
    return INDEX.read_text(encoding="utf-8")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(user_id: int = Depends(require_admin)) -> str:
    return ADMIN.read_text(encoding="utf-8")


@app.get("/api/admin/users")
def list_users(user_id: int = Depends(require_admin)) -> dict:
    return {"items": database.list_users()}


@app.get("/api/settings")
def app_settings(user_id: int = Depends(require_user)) -> dict:
    """Expose only the client-facing runtime limits needed by the web UI."""
    return {
        "max_ids_per_task": max(1, int(config.get("download", {}).get("max_ids_per_task", 20))),
    }


@app.post("/api/admin/users", status_code=201)
def create_user(request: UserCreateRequest, user_id: int = Depends(require_admin)) -> dict:
    try:
        new_user_id = database.create_user(request.token, request.note, request.enabled)
        return {"id": new_user_id}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/api/admin/users/{target_user_id}")
def update_user(
    target_user_id: int,
    request: UserUpdateRequest,
    user_id: int = Depends(require_admin),
) -> dict:
    try:
        if not database.update_user(target_user_id, request.note, request.enabled, request.token):
            raise HTTPException(status_code=404, detail="用户不存在")
        return {"updated": True}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/admin/users/{target_user_id}", status_code=204)
def remove_user(target_user_id: int, user_id: int = Depends(require_admin)) -> None:
    try:
        if not database.delete_user(target_user_id):
            raise HTTPException(status_code=404, detail="用户不存在")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    for root in (DOWNLOAD_ROOT, ARCHIVE_ROOT):
        shutil.rmtree(root / str(target_user_id), ignore_errors=True)


@app.post("/api/preview")
def get_preview(request: IdRequest, user_id: int = Depends(require_user)) -> dict:
    try:
        ids = normalize_ids(request.ids)
        return {"items": preview(ids)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/search")
def search_albums(request: SearchRequest, user_id: int = Depends(require_user)) -> dict:
    try:
        return search(
            request.query,
            request.page,
            request.search_type,
            request.order_by,
            request.time,
            request.category,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks", status_code=202)
def create_task(request: IdRequest, user_id: int = Depends(require_user)) -> dict:
    try:
        ids = normalize_ids(request.ids)
        task = manager.create(user_id, ids)
        return {"task_id": task.task_id, "status": task.status}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str, user_id: int = Depends(require_user)) -> dict:
    task = manager.get(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task_id": task.task_id, "ids": task.ids, "status": task.status,
            "message": task.message, "completed": task.completed, "total": task.total,
            "archive": task.archive, "error": task.error}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, user_id: int = Depends(require_user)) -> StreamingResponse:
    task = manager.get(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def stream():
        initial = task.snapshot()
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
        if initial.get("status") in {"completed", "failed"}:
            return
        while True:
            event = await manager.next_event(task)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("status") in {"completed", "failed"}:
                break

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/tasks/{task_id}/file")
def download_file(task_id: str, user_id: int = Depends(require_user)) -> FileResponse:
    task = manager.get(task_id, user_id)
    if task is None or task.status != "completed" or not task.archive:
        raise HTTPException(status_code=404, detail="文件尚未准备好")
    archive = Path(task.archive)
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(archive, media_type="application/zip", filename=archive.name)
