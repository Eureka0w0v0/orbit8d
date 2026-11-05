"""HTTP 接口（SPEC §7）。只监听 127.0.0.1；Host 白名单防 DNS 重绑定；跨站的写请求一律拒绝。"""

import hashlib
import io
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
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from orbit8d import __version__
from orbit8d.assets import ensure_hrtf_sofa, prune_stale_assets
from orbit8d.config import REPO_ROOT, SAMPLE_RATE, Settings
from orbit8d.engine.choreo import choreograph, rechoreograph
from orbit8d.engine.hrtf import load_grid, to_bytes
from orbit8d.engine.pipeline import RENDER_VERSION, Analysis, Renderer
from orbit8d.engine.reverb import BRIR_VERSION, ROOMS
from orbit8d.engine.scene import PRESETS, Scene, canonical_json, preset
from orbit8d.engine.structure import SectionInfo
from orbit8d.jobs.states import ExportState, ProjectState, is_terminal
from orbit8d.jobs.store import UPLOAD_SUFFIX, ExportRecord, NotFound, ProjectRecord, Store
from orbit8d.jobs.worker import (
    ORIGINAL_PREVIEW,
    PREVIEW_DIR,
    SOURCE_FILE,
    JobRunner,
    Separator,
    safe_name,
    song_title,
)
from orbit8d.media.ffmpeg import OUTPUT_FORMATS, MediaError, available_formats, probe

log = logging.getLogger(__name__)

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
DEV_WEB_PORT = 5173
ID_LEN = 16
FILENAME_HEADER = "x-filename"
PREVIEW_NAMES = frozenset(
    {"vocals_hi", "bass_hi", "drums_hi", "other_hi", "bass_sub", "drums_sub", "other_sub", ORIGINAL_PREVIEW}
)
MAX_SCENE_BYTES = 256 * 1024  # 场景 JSON 最多约 30 KB（16 段 × 4 轨 + 64 个事件），留足余量
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
    loudness: Literal["loud", "standard"] = "loud"  # 响亮（与试听一致）/ 标准（-14 LUFS）


class SceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene


class LocalOriginMiddleware:
    """浏览器发起的跨站写请求一定带 Origin；不在白名单里的直接 403（防 CSRF）。"""

    def __init__(self, app: ASGIApp, allowed_origins: set[str]):
        self.app, self.allowed = app, allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin.decode("latin-1") not in self.allowed:
                body = {"code": "FORBIDDEN_ORIGIN", "message": "Cross-site request refused"}
                await JSONResponse(body, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def housekeeping(store: Store, settings: Settings) -> None:
    """启动时清理：回收目录、没传完的上传、不再使用的旧缓存。"""
    store.empty_trash()
    uploads = store.clean_uploads()
    assets = prune_stale_assets(settings.assets_dir)
    log.info("housekeeping done", extra={"event": "server.cleanup", "uploads": uploads, "assets": assets})


def project_summary(store: Store, rec: ProjectRecord) -> dict:
    return {
        "id": rec.id,
        "state": str(rec.state),
        "title": song_title(rec.source),
        "artist": rec.source.get("artist", ""),
        "duration_s": rec.source.get("duration_s", 0.0),
        "size_bytes": store.disk_usage(rec.id),
        "error": rec.error,
        "last_used": store.last_used(rec),
    }


def create_app(settings: Settings, separator: Separator) -> FastAPI:
    store = Store(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        grid = await run_in_threadpool(
            load_grid, ensure_hrtf_sofa(settings.assets_dir), settings.assets_dir, SAMPLE_RATE
        )
        renderer = Renderer(grid)
        runner = JobRunner(store, renderer, separator)
        housekeeping(store, settings)
        store.recover_interrupted()
        runner.migrate_storage()  # 先迁移存储，再按需重新分析
        runner.refresh_stale()
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
        return JSONResponse({"code": "NOT_FOUND", "message": "Not found"}, status_code=404)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "formats": available_formats()}

    @app.get("/api/presets")
    def list_presets() -> list[str]:
        return list(PRESETS)

    @app.get("/api/presets/{name}")
    def get_preset(name: str, bars: int = 4) -> dict:
        if name not in PRESETS:
            raise NotFound(name)
        if bars not in BAR_CHOICES:
            raise ApiError(422, "BAD_BARS", f"bars must be one of {BAR_CHOICES}")
        return preset(name, bars).model_dump(mode="json")

    @app.get("/api/scene/schema")
    def scene_schema() -> dict:
        return Scene.model_json_schema()

    @app.post("/api/projects")
    async def create_project(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes:
            raise ApiError(413, "TOO_LARGE", "The file is too large")
        name = safe_name(Path(unquote(request.headers.get(FILENAME_HEADER, ""))).name)
        tmp = settings.uploads_dir / f"{uuid.uuid4().hex}{UPLOAD_SUFFIX}"
        digest, size = hashlib.sha256(), 0
        try:
            with tmp.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise ApiError(413, "TOO_LARGE", "The file is too large")
                    digest.update(chunk)
                    f.write(chunk)
            if size == 0:
                raise ApiError(400, "EMPTY", "The file is empty")
            pid = digest.hexdigest()[:ID_LEN]
            existing = store.find_project(pid)
            info = None if existing else await run_in_threadpool(probe, tmp)
            if info and info.duration_s > settings.max_duration_s:
                raise ApiError(
                    400, "TOO_LONG", f"The song is longer than {settings.max_duration_s / 60:.0f} minutes"
                )
            with store.lock:  # 与删歌互斥：查记录、建记录、排任务一起做
                rec, status = register_upload(request, pid, tmp, info, name, size, digest.hexdigest())
            log.info("project uploaded", extra={"event": "project.upload", "project_id": pid, "bytes": size})
            return JSONResponse(rec.to_dict(), status_code=status)
        finally:
            tmp.unlink(missing_ok=True)

    def register_upload(
        request: Request, pid: str, tmp: Path, info, name: str, size: int, sha256: str
    ) -> tuple[ProjectRecord, int]:
        """已有这首歌：失败过就重试，返回 200；没有：建项目、排分析，返回 202。"""
        runner = request.app.state.runner
        existing = store.find_project(pid)
        if existing:
            if existing.state is ProjectState.FAILED and not runner.busy(pid):
                runner.submit("analysis", pid, pid)
            return existing, 200
        if info is None:  # 查的时候还在、加锁前被删了：按新歌再来一次
            raise ApiError(409, "PROJECT_BUSY", "The song was just deleted; import it again")
        store.project_dir(pid).mkdir(parents=True, exist_ok=True)
        tmp.replace(store.project_dir(pid) / SOURCE_FILE)
        rec, _ = store.create_project_if_absent(
            ProjectRecord(
                id=pid,
                state=ProjectState.UPLOADED,
                source={
                    "filename": name,
                    "size": size,
                    "sha256": sha256,
                    "codec": info.codec,
                    "duration_s": info.duration_s,
                    "title": info.title,
                    "artist": info.artist,
                    "album": info.album,
                    "has_cover": info.has_cover,
                },
            )
        )
        runner.submit("analysis", pid, pid)
        return rec, 202

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        """最近用过的歌在前：标题、时长、状态、占用空间。"""
        items = [project_summary(store, rec) for rec in store.list_projects()]
        return sorted(items, key=lambda item: item["last_used"], reverse=True)

    @app.delete("/api/projects/{pid}", status_code=204)
    def delete_project(request: Request, pid: str) -> Response:
        """连同它的导出一起删。还有任务（分析中、排队导出等）时 409，不删半截。"""
        with store.lock:
            rec = store.get_project(pid)
            if request.app.state.runner.busy(pid) or not is_terminal(rec.state):
                raise ApiError(409, "PROJECT_BUSY", "The song is still being processed")
            moved = store.trash_project(pid)
        store.empty_trash(moved)
        return Response(status_code=204)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        return store.get_project(pid).to_dict()

    def ready_analysis(pid: str) -> Analysis:
        rec = store.get_project(pid)
        if rec.state is not ProjectState.READY:
            raise ApiError(409, "PROJECT_NOT_READY", "Still processing")
        return Analysis(**rec.analysis)

    @app.get("/api/projects/{pid}/choreography")
    def get_choreography(pid: str) -> dict:
        """按自动识别的段落编排好的场景（前端“自动编排”按钮、新项目的默认场景）。"""
        a = ready_analysis(pid)
        sections = [SectionInfo(**d) for d in a.sections]
        return choreograph(sections, a.default_bars, a.bpm_norm).model_dump(mode="json")

    @app.post("/api/projects/{pid}/choreography")
    def post_choreography(pid: str, body: SceneRequest) -> dict:
        """✨ 自动编排：沿用这个场景的分界与段名重新编排（只换各段轨道、混响量与事件）。"""
        a = ready_analysis(pid)
        scene = rechoreograph(
            body.scene, a.duration_s, a.default_bars, a.bpm_norm, a.envelope_db, a.envelope_hop_s
        )
        return scene.model_dump(mode="json")

    @app.post("/api/projects/{pid}/eq")
    def post_scene_eq(request: Request, pid: str, body: SceneRequest) -> Response:
        """这个场景的音色补偿 EQ（单声道 float32 WAV 冲激响应），浏览器试听与导出用同一个。"""
        fir = request.app.state.renderer.scene_eq(body.scene, ready_analysis(pid))
        buf = io.BytesIO()
        sf.write(buf, fir.astype("float32"), SAMPLE_RATE, format="WAV", subtype="FLOAT")
        return Response(buf.getvalue(), media_type="audio/wav")

    @app.get("/api/projects/{pid}/scene")
    def get_scene(pid: str) -> dict:
        """用户上次保存的场景；没保存过（或旧文件已不合法）时 404，前端改用自动编排。"""
        ready_analysis(pid)
        text = store.load_scene(pid)
        if text is None:
            raise ApiError(404, "NO_SAVED_SCENE", "No scene has been saved for this song")
        try:
            return Scene.model_validate_json(text).model_dump(mode="json")
        except ValidationError:
            log.warning("saved scene is invalid", extra={"event": "scene.invalid", "project_id": pid})
            raise ApiError(404, "NO_SAVED_SCENE", "The saved scene is no longer valid") from None

    @app.put("/api/projects/{pid}/scene", status_code=204)
    async def put_scene(request: Request, pid: str) -> Response:
        """整份替换保存（幂等）；先按大小上限拒绝，再按场景白名单校验。"""
        ready_analysis(pid)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_SCENE_BYTES:
            raise ApiError(413, "TOO_LARGE", "The scene is too large")
        raw = await request.body()
        if len(raw) > MAX_SCENE_BYTES:
            raise ApiError(413, "TOO_LARGE", "The scene is too large")
        try:
            scene = Scene.model_validate_json(raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            raise ApiError(
                422, "INVALID_SCENE", f"Invalid scene: {where} {first.get('msg', '')}".strip()
            ) from None
        store.save_scene(pid, canonical_json(scene))
        log.info("scene saved", extra={"event": "scene.save", "project_id": pid, "bytes": len(raw)})
        return Response(status_code=204)

    @app.get("/api/projects/{pid}/stems/{name}.flac")
    def get_stem(pid: str, name: str) -> FileResponse:
        rec = store.get_project(pid)
        if name not in PREVIEW_NAMES:
            raise NotFound(name)
        if rec.state is not ProjectState.READY:
            raise ApiError(409, "PROJECT_NOT_READY", "Still processing")
        path = store.project_dir(pid) / PREVIEW_DIR / f"{name}.flac"
        if not path.exists():
            raise NotFound(name)
        return FileResponse(path, media_type="audio/flac")

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

    @app.get("/api/assets/brir/{room}.wav")
    def get_brir(request: Request, room: str) -> FileResponse:
        if room not in ROOMS:
            raise NotFound(room)
        path = cached_wav(
            f"brir_v{BRIR_VERSION}_{room}_{SAMPLE_RATE}.wav", lambda: request.app.state.renderer.brir(room).T
        )
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/projects/{pid}/exports")
    def create_export(request: Request, pid: str, body: ExportRequest) -> JSONResponse:
        with store.lock:  # 与删歌互斥：确认歌还在、建导出记录、排任务一起做
            return queue_export(request, pid, body)

    def queue_export(request: Request, pid: str, body: ExportRequest) -> JSONResponse:
        project = store.get_project(pid)
        if project.state is not ProjectState.READY:
            raise ApiError(409, "PROJECT_NOT_READY", "Still processing")
        if body.format not in available_formats():
            raise ApiError(422, "UNSUPPORTED_FORMAT", f"The local ffmpeg cannot encode {body.format}")
        key = f"v{RENDER_VERSION}|{pid}|{canonical_json(body.scene)}|{body.format}|{body.loudness}"
        eid = hashlib.sha256(key.encode()).hexdigest()[:ID_LEN]
        rec, created = store.create_export_if_absent(
            ExportRecord(
                id=eid,
                project_id=pid,
                state=ExportState.QUEUED,
                format=body.format,
                scene=body.scene.model_dump(mode="json"),
                loudness=body.loudness,
            )
        )
        if not created and rec.state is ExportState.FAILED:
            rec = store.transition_export(eid, ExportState.QUEUED)
            created = True
        if created:
            request.app.state.runner.submit("export", eid, pid)
        return JSONResponse(rec.to_dict(), status_code=202 if created else 200)

    @app.get("/api/exports/{eid}")
    def get_export(eid: str) -> dict:
        return store.get_export(eid).to_dict()

    @app.get("/api/exports/{eid}/file")
    def get_export_file(eid: str) -> FileResponse:
        rec = store.get_export(eid)
        if rec.state is not ExportState.DONE:
            raise ApiError(409, "EXPORT_NOT_READY", "The export is not finished yet")
        fmt = OUTPUT_FORMATS[rec.format]
        return FileResponse(
            store.export_dir(eid) / f"output.{fmt.ext}", media_type=fmt.mime, filename=rec.file_name
        )

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
