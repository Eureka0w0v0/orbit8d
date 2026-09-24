"""HTTP 接口（SPEC §7）。只监听 127.0.0.1；Host 白名单防 DNS 重绑定；跨站的写请求一律拒绝。"""

import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

import soundfile as sf
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from orbit8d import __version__
from orbit8d.assets import ensure_hrtf_sofa
from orbit8d.config import REPO_ROOT, SAMPLE_RATE, Settings
from orbit8d.engine.hrtf import load_grid, to_bytes
from orbit8d.engine.pipeline import Renderer
from orbit8d.engine.reverb import ROOMS
from orbit8d.engine.scene import PRESETS, Scene, canonical_json, preset
from orbit8d.jobs.states import ExportState, ProjectState
from orbit8d.jobs.store import ExportRecord, NotFound, ProjectRecord, Store
from orbit8d.jobs.worker import PREVIEW_DIR, SOURCE_FILE, JobRunner, Separator, safe_name
from orbit8d.media.ffmpeg import OUTPUT_FORMATS, MediaError, available_formats, probe

log = logging.getLogger(__name__)

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
DEV_WEB_PORT = 5173
ID_LEN = 16
FILENAME_HEADER = "x-filename"
PREVIEW_NAMES = frozenset(
    {"vocals_hi", "bass_hi", "drums_hi", "other_hi", "bass_sub", "drums_sub", "other_sub"}
)
BAR_CHOICES = (1, 2, 4, 8)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WEB_DIST = REPO_ROOT / "web" / "dist"


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene
    format: Literal["m4a", "mp3", "flac", "wav", "ogg"]


class LocalOriginMiddleware:
    """浏览器发起的跨站写请求一定带 Origin；不在白名单里的直接 403（防 CSRF）。"""

    def __init__(self, app: ASGIApp, allowed_origins: set[str]):
        self.app, self.allowed = app, allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin.decode("latin-1") not in self.allowed:
                body = {"code": "FORBIDDEN_ORIGIN", "message": "拒绝跨站请求"}
                await JSONResponse(body, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings, separator: Separator) -> FastAPI:
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        grid = await run_in_threadpool(
            load_grid, ensure_hrtf_sofa(settings.assets_dir), settings.assets_dir, SAMPLE_RATE
        )
        renderer = Renderer(grid)
        runner = JobRunner(store, renderer, separator)
        store.recover_interrupted()
        runner.start()
        app.state.store, app.state.runner, app.state.renderer = store, runner, renderer
        app.state.hrtf_bytes = to_bytes(grid)
        log.info("server ready", extra={"event": "server.ready", "port": settings.port})
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(
        title="Orbit 8D",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    origins = {f"http://{host}:{port}" for host in ALLOWED_HOSTS for port in (settings.port, DEV_WEB_PORT)}
    app.add_middleware(LocalOriginMiddleware, allowed_origins=origins)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)  # 最外层

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(MediaError)
    async def _media_error(_: Request, exc: MediaError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=400)

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"code": "NOT_FOUND", "message": "不存在"}, status_code=404)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "formats": available_formats()}

    @app.get("/api/presets/{name}")
    def get_preset(name: str, bars: int = 4) -> dict:
        if name not in PRESETS:
            raise NotFound(name)
        if bars not in BAR_CHOICES:
            raise ApiError(422, "BAD_BARS", f"bars 只能是 {BAR_CHOICES}")
        return preset(name, bars).model_dump(mode="json")

    @app.get("/api/scene/schema")
    def scene_schema() -> dict:
        return Scene.model_json_schema()

    @app.post("/api/projects")
    async def create_project(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
            raise ApiError(413, "TOO_LARGE", "文件太大")
        name = safe_name(Path(unquote(request.headers.get(FILENAME_HEADER, ""))).name)
        tmp = settings.uploads_dir / f"{uuid.uuid4().hex}.part"
        digest, size = hashlib.sha256(), 0
        try:
            with tmp.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise ApiError(413, "TOO_LARGE", "文件太大")
                    digest.update(chunk)
                    f.write(chunk)
            if size == 0:
                raise ApiError(400, "EMPTY", "空文件")
            pid = digest.hexdigest()[:ID_LEN]
            existing = store.find_project(pid)
            if existing:
                if existing.state is ProjectState.FAILED:
                    request.app.state.runner.submit("analysis", pid)
                return JSONResponse(existing.to_dict(), status_code=200)
            info = await run_in_threadpool(probe, tmp)
            if info.duration_s > settings.max_duration_s:
                raise ApiError(400, "TOO_LONG", f"歌曲超过 {settings.max_duration_s / 60:.0f} 分钟")
            store.project_dir(pid).mkdir(parents=True, exist_ok=True)
            tmp.replace(store.project_dir(pid) / SOURCE_FILE)
            rec, created = store.create_project_if_absent(
                ProjectRecord(
                    id=pid,
                    state=ProjectState.UPLOADED,
                    source={
                        "filename": name,
                        "size": size,
                        "sha256": digest.hexdigest(),
                        "codec": info.codec,
                        "duration_s": info.duration_s,
                        "title": info.title,
                        "artist": info.artist,
                        "album": info.album,
                        "has_cover": info.has_cover,
                    },
                )
            )
            if created:
                request.app.state.runner.submit("analysis", pid)
            log.info("project uploaded", extra={"event": "project.upload", "project_id": pid, "bytes": size})
            return JSONResponse(rec.to_dict(), status_code=202 if created else 200)
        finally:
            tmp.unlink(missing_ok=True)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        return store.get_project(pid).to_dict()

    @app.get("/api/projects/{pid}/stems/{name}.flac")
    def get_stem(pid: str, name: str) -> FileResponse:
        rec = store.get_project(pid)
        if name not in PREVIEW_NAMES:
            raise NotFound(name)
        if rec.state is not ProjectState.READY:
            raise ApiError(409, "PROJECT_NOT_READY", "还在处理中")
        return FileResponse(store.project_dir(pid) / PREVIEW_DIR / f"{name}.flac", media_type="audio/flac")

    @app.get("/api/assets/hrtf.bin")
    def get_hrtf(request: Request) -> Response:
        return Response(request.app.state.hrtf_bytes, media_type="application/octet-stream")

    def cached_wav(name: str, make) -> Path:
        path = settings.assets_dir / name
        if not path.exists():
            tmp = path.with_suffix(".tmp.wav")
            sf.write(tmp, make(), SAMPLE_RATE, subtype="FLOAT")
            tmp.replace(path)
        return path

    @app.get("/api/assets/eq.wav")
    def get_eq(request: Request) -> FileResponse:
        path = cached_wav(f"eq_{SAMPLE_RATE}.wav", lambda: request.app.state.renderer.eq.astype("float32"))
        return FileResponse(path, media_type="audio/wav")

    @app.get("/api/assets/brir/{room}.wav")
    def get_brir(request: Request, room: str) -> FileResponse:
        if room not in ROOMS:
            raise NotFound(room)
        path = cached_wav(f"brir_{room}_{SAMPLE_RATE}.wav", lambda: request.app.state.renderer.brir(room).T)
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/projects/{pid}/exports")
    def create_export(request: Request, pid: str, body: ExportRequest) -> JSONResponse:
        project = store.get_project(pid)
        if project.state is not ProjectState.READY:
            raise ApiError(409, "PROJECT_NOT_READY", "还在处理中")
        if body.format not in available_formats():
            raise ApiError(422, "UNSUPPORTED_FORMAT", f"本机 ffmpeg 不支持 {body.format}")
        key = f"{pid}|{canonical_json(body.scene)}|{body.format}"
        eid = hashlib.sha256(key.encode()).hexdigest()[:ID_LEN]
        rec, created = store.create_export_if_absent(
            ExportRecord(
                id=eid,
                project_id=pid,
                state=ExportState.QUEUED,
                format=body.format,
                scene=body.scene.model_dump(mode="json"),
            )
        )
        if not created and rec.state is ExportState.FAILED:
            rec = store.transition_export(eid, ExportState.QUEUED)
            created = True
        if created:
            request.app.state.runner.submit("export", eid)
        return JSONResponse(rec.to_dict(), status_code=202 if created else 200)

    @app.get("/api/exports/{eid}")
    def get_export(eid: str) -> dict:
        return store.get_export(eid).to_dict()

    @app.get("/api/exports/{eid}/file")
    def get_export_file(eid: str) -> FileResponse:
        rec = store.get_export(eid)
        if rec.state is not ExportState.DONE:
            raise ApiError(409, "EXPORT_NOT_READY", "还没导出完成")
        fmt = OUTPUT_FORMATS[rec.format]
        return FileResponse(
            store.export_dir(eid) / f"output.{fmt.ext}", media_type=fmt.mime, filename=rec.file_name
        )

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
