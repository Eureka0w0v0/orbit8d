"""歌（项目）相关的接口（SPEC §7）：上传、列表、状态、删除、试听文件。"""

import hashlib
import logging
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

from orbit8d.api.guards import ApiError
from orbit8d.api.state import songs_of
from orbit8d.config import Settings
from orbit8d.jobs.songs import Upload
from orbit8d.jobs.stems import PREVIEW_NAMES
from orbit8d.jobs.store import NotFound, ProjectRecord, Store, safe_name

log = logging.getLogger(__name__)

FILENAME_HEADER = "x-filename"


def project_summary(store: Store, rec: ProjectRecord) -> dict:
    return {
        "id": rec.id,
        "state": str(rec.state),
        "title": rec.source.song_title,
        "artist": rec.source.artist,
        "duration_s": rec.source.duration_s,
        "size_bytes": store.disk_usage(rec.id),
        "error": rec.error,
        "last_used": store.last_used(rec),
    }


async def receive(request: Request, target: Path, limit: int) -> tuple[int, str]:
    """请求体边收边写进 target，返回 (字节数, sha256)；一超过 limit 就中止。"""
    digest, size = hashlib.sha256(), 0
    with target.open("wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise ApiError(413, "TOO_LARGE", "The file is too large")
            digest.update(chunk)
            f.write(chunk)
    return size, digest.hexdigest()


def projects_router(store: Store, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post("/api/projects")
    async def create_project(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
            raise ApiError(413, "TOO_LARGE", "The file is too large")
        name = safe_name(Path(unquote(request.headers.get(FILENAME_HEADER, ""))).name)
        tmp = store.new_upload()
        try:
            size, sha256 = await receive(request, tmp, settings.max_upload_bytes)
            if size == 0:
                raise ApiError(400, "EMPTY", "The file is empty")
            rec, created = await run_in_threadpool(songs_of(request).add, Upload(tmp, name, size, sha256))
            extra = {"event": "project.upload", "project_id": rec.id, "bytes": size}
            log.info("project uploaded", extra=extra)
            return JSONResponse(rec.to_dict(), status_code=202 if created else 200)
        finally:
            tmp.unlink(missing_ok=True)

    @router.get("/api/projects")
    def list_projects() -> list[dict]:
        """最近用过的歌在前：标题、时长、状态、占用空间。"""
        items = [project_summary(store, rec) for rec in store.list_projects()]
        return sorted(items, key=lambda item: item["last_used"], reverse=True)

    @router.delete("/api/projects/{pid}", status_code=204)
    def delete_project(request: Request, pid: str) -> Response:
        """连同它的导出一起删。还有任务（分析中、排队导出等）时 409，不删半截。"""
        songs_of(request).delete(pid)
        return Response(status_code=204)

    @router.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        return store.get_project(pid).to_dict()

    @router.get("/api/projects/{pid}/stems/{name}.flac")
    def get_stem(request: Request, pid: str, name: str) -> FileResponse:
        store.get_project(pid)  # 先确认歌在（404），再看文件名（404），最后才看是否就绪（409）
        if name not in PREVIEW_NAMES:
            raise NotFound(name)
        songs_of(request).ready(pid)
        path = store.project_files(pid).preview(name)
        if not path.exists():
            raise NotFound(name)
        return FileResponse(path, media_type="audio/flac")

    return router
