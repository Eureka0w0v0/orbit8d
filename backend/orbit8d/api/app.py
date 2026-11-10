"""HTTP 接口（SPEC §7）。只监听 127.0.0.1；Host 与跨站防护、错误应答的格式见 api/guards.py。"""

import hashlib
import io
import logging
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

from orbit8d import __version__
from orbit8d.api.guards import ApiError, install_guards
from orbit8d.assets import cached_brir, ensure_hrtf_sofa, prune_stale_assets
from orbit8d.config import REPO_ROOT, SAMPLE_RATE, Settings
from orbit8d.engine.choreo import choreograph, rechoreograph
from orbit8d.engine.hrtf import load_grid, to_bytes
from orbit8d.engine.orbit import BAR_CHOICES
from orbit8d.engine.pipeline import Renderer
from orbit8d.engine.reverb import ROOMS
from orbit8d.engine.scene import PRESETS, Scene, canonical_json, preset
from orbit8d.engine.structure import SectionInfo
from orbit8d.jobs.layout import ORIGINAL_PREVIEW
from orbit8d.jobs.songs import Songs, Upload
from orbit8d.jobs.states import ExportState
from orbit8d.jobs.store import NotFound, ProjectRecord, Store, safe_name
from orbit8d.jobs.worker import JobRunner, Separator
from orbit8d.media.ffmpeg import OUTPUT_FORMATS, available_formats

log = logging.getLogger(__name__)

FILENAME_HEADER = "x-filename"
PREVIEW_NAMES = frozenset(
    {"vocals_hi", "bass_hi", "drums_hi", "other_hi", "bass_sub", "drums_sub", "other_sub", ORIGINAL_PREVIEW}
)
MAX_SCENE_BYTES = 256 * 1024  # 场景 JSON 最多约 30 KB（16 段 × 4 轨 + 64 个事件），留足余量
WEB_DIST = REPO_ROOT / "web" / "dist"


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene
    format: Literal["m4a", "mp3", "flac", "wav", "ogg"]
    loudness: Literal["loud", "standard"] = "loud"  # 响亮（与试听一致）/ 标准（-14 LUFS）


class SceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene


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
        "title": rec.source.song_title,
        "artist": rec.source.artist,
        "duration_s": rec.source.duration_s,
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
        app.state.songs = Songs(store, runner, settings.max_duration_s)
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
    install_guards(app, settings.port)

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
        tmp = store.new_upload()
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
            songs: Songs = request.app.state.songs
            rec, created = await run_in_threadpool(songs.add, Upload(tmp, name, size, digest.hexdigest()))
            extra = {"event": "project.upload", "project_id": rec.id, "bytes": size}
            log.info("project uploaded", extra=extra)
            return JSONResponse(rec.to_dict(), status_code=202 if created else 200)
        finally:
            tmp.unlink(missing_ok=True)

    @app.get("/api/projects")
    def list_projects() -> list[dict]:
        """最近用过的歌在前：标题、时长、状态、占用空间。"""
        items = [project_summary(store, rec) for rec in store.list_projects()]
        return sorted(items, key=lambda item: item["last_used"], reverse=True)

    @app.delete("/api/projects/{pid}", status_code=204)
    def delete_project(request: Request, pid: str) -> Response:
        """连同它的导出一起删。还有任务（分析中、排队导出等）时 409，不删半截。"""
        request.app.state.songs.delete(pid)
        return Response(status_code=204)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        return store.get_project(pid).to_dict()

    @app.get("/api/projects/{pid}/choreography")
    def get_choreography(request: Request, pid: str) -> dict:
        """按自动识别的段落编排好的场景（前端“自动编排”按钮、新项目的默认场景）。"""
        a = request.app.state.songs.analysis(pid)
        sections = [SectionInfo(**d) for d in a.sections]
        return choreograph(sections, a.default_bars, a.bpm_norm).model_dump(mode="json")

    @app.post("/api/projects/{pid}/choreography")
    def post_choreography(request: Request, pid: str, body: SceneRequest) -> dict:
        """✨ 自动编排：沿用这个场景的分界与段名重新编排（只换各段轨道、混响量与事件）。"""
        a = request.app.state.songs.analysis(pid)
        scene = rechoreograph(
            body.scene, a.duration_s, a.default_bars, a.bpm_norm, a.envelope_db, a.envelope_hop_s
        )
        return scene.model_dump(mode="json")

    @app.post("/api/projects/{pid}/eq")
    def post_scene_eq(request: Request, pid: str, body: SceneRequest) -> Response:
        """这个场景的音色补偿 EQ（单声道 float32 WAV 冲激响应），浏览器试听与导出用同一个。"""
        fir = request.app.state.renderer.scene_eq(body.scene, request.app.state.songs.analysis(pid))
        buf = io.BytesIO()
        sf.write(buf, fir.astype("float32"), SAMPLE_RATE, format="WAV", subtype="FLOAT")
        return Response(buf.getvalue(), media_type="audio/wav")

    @app.get("/api/projects/{pid}/scene")
    def get_scene(request: Request, pid: str) -> dict:
        """用户上次保存的场景；没保存过（或旧文件已不合法）时 404，前端改用自动编排。"""
        request.app.state.songs.ready(pid)
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
        request.app.state.songs.ready(pid)
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
    def get_stem(request: Request, pid: str, name: str) -> FileResponse:
        store.get_project(pid)  # 先确认歌在（404），再看文件名（404），最后才看是否就绪（409）
        if name not in PREVIEW_NAMES:
            raise NotFound(name)
        request.app.state.songs.ready(pid)
        path = store.project_files(pid).preview(name)
        if not path.exists():
            raise NotFound(name)
        return FileResponse(path, media_type="audio/flac")

    @app.get("/api/assets/hrtf.bin")
    def get_hrtf(request: Request) -> Response:
        return Response(request.app.state.hrtf_bytes, media_type="application/octet-stream")

    @app.get("/api/assets/brir/{room}.wav")
    def get_brir(request: Request, room: str) -> FileResponse:
        if room not in ROOMS:
            raise NotFound(room)
        renderer = request.app.state.renderer
        path = cached_brir(settings.assets_dir, room, SAMPLE_RATE, lambda: renderer.brir(room).T)
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/projects/{pid}/exports")
    def create_export(request: Request, pid: str, body: ExportRequest) -> JSONResponse:
        songs: Songs = request.app.state.songs
        rec, queued = songs.queue_export(pid, body.scene, body.format, body.loudness)
        return JSONResponse(rec.to_dict(), status_code=202 if queued else 200)

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
            store.export_files(eid).output(fmt.ext), media_type=fmt.mime, filename=rec.file_name
        )

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
    return app
