"""导出相关的接口（SPEC §7）：提交导出（幂等）、查状态、下载。"""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from orbit8d.api.guards import ApiError
from orbit8d.api.state import songs_of
from orbit8d.engine.scene import Scene
from orbit8d.jobs.states import ExportState
from orbit8d.jobs.store import Store
from orbit8d.media.ffmpeg import OUTPUT_FORMATS


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene
    format: Literal["m4a", "mp3", "flac", "wav", "ogg"]
    loudness: Literal["loud", "standard"] = "loud"  # 响亮（与试听一致）/ 标准（-14 LUFS）


def exports_router(store: Store) -> APIRouter:
    router = APIRouter()

    @router.post("/api/projects/{pid}/exports")
    def create_export(request: Request, pid: str, body: ExportRequest) -> JSONResponse:
        rec, queued = songs_of(request).queue_export(pid, body.scene, body.format, body.loudness)
        return JSONResponse(rec.to_dict(), status_code=202 if queued else 200)

    @router.get("/api/exports/{eid}")
    def get_export(eid: str) -> dict:
        return store.get_export(eid).to_dict()

    @router.get("/api/exports/{eid}/file")
    def get_export_file(eid: str) -> FileResponse:
        rec = store.get_export(eid)
        if rec.state is not ExportState.DONE:
            raise ApiError(409, "EXPORT_NOT_READY", "The export is not finished yet")
        fmt = OUTPUT_FORMATS[rec.format]
        return FileResponse(
            store.export_files(eid).output(fmt.ext), media_type=fmt.mime, filename=rec.file_name
        )

    return router
