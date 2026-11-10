"""健康检查，以及浏览器试听要下载的 DSP 数据：HRTF 表与各房间的混响冲激响应（SPEC §7）。"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from orbit8d import __version__
from orbit8d.api.state import renderer_of
from orbit8d.assets import cached_brir
from orbit8d.config import SAMPLE_RATE, Settings
from orbit8d.engine.reverb import ROOMS
from orbit8d.jobs.store import NotFound
from orbit8d.media.ffmpeg import available_formats


def assets_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "formats": available_formats()}

    @router.get("/api/assets/hrtf.bin")
    def get_hrtf(request: Request) -> Response:
        return Response(request.app.state.hrtf_bytes, media_type="application/octet-stream")

    @router.get("/api/assets/brir/{room}.wav")
    def get_brir(request: Request, room: str) -> FileResponse:
        if room not in ROOMS:
            raise NotFound(room)
        renderer = renderer_of(request)
        path = cached_brir(settings.assets_dir, room, SAMPLE_RATE, lambda: renderer.brir(room).T)
        return FileResponse(path, media_type="audio/wav")

    return router
