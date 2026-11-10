"""场景相关的接口（SPEC §7）：预设、参数表、自动编排、按场景的补偿 EQ、每首歌保存的场景。"""

import io
import logging

import soundfile as sf
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from orbit8d.api.guards import ApiError
from orbit8d.api.state import renderer_of, songs_of
from orbit8d.config import SAMPLE_RATE
from orbit8d.engine.choreo import choreograph, rechoreograph
from orbit8d.engine.orbit import BAR_CHOICES
from orbit8d.engine.scene import PRESETS, Scene, canonical_json, preset
from orbit8d.engine.structure import SectionInfo
from orbit8d.jobs.store import NotFound, Store

log = logging.getLogger(__name__)

MAX_SCENE_BYTES = 256 * 1024  # 场景 JSON 最多约 30 KB（16 段 × 4 轨 + 64 个事件），留足余量


class SceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: Scene


def scene_router(store: Store) -> APIRouter:
    router = APIRouter()

    @router.get("/api/presets")
    def list_presets() -> list[str]:
        return list(PRESETS)

    @router.get("/api/presets/{name}")
    def get_preset(name: str, bars: int = 4) -> dict:
        if name not in PRESETS:
            raise NotFound(name)
        if bars not in BAR_CHOICES:
            raise ApiError(422, "BAD_BARS", f"bars must be one of {BAR_CHOICES}")
        return preset(name, bars).model_dump(mode="json")

    @router.get("/api/scene/schema")
    def scene_schema() -> dict:
        return Scene.model_json_schema()

    @router.get("/api/projects/{pid}/choreography")
    def get_choreography(request: Request, pid: str) -> dict:
        """按自动识别的段落编排好的场景（前端“自动编排”按钮、新项目的默认场景）。"""
        a = songs_of(request).analysis(pid)
        sections = [SectionInfo(**d) for d in a.sections]
        return choreograph(sections, a.default_bars, a.bpm_norm).model_dump(mode="json")

    @router.post("/api/projects/{pid}/choreography")
    def post_choreography(request: Request, pid: str, body: SceneRequest) -> dict:
        """✨ 自动编排：沿用这个场景的分界与段名重新编排（只换各段轨道、混响量与事件）。"""
        a = songs_of(request).analysis(pid)
        scene = rechoreograph(
            body.scene, a.duration_s, a.default_bars, a.bpm_norm, a.envelope_db, a.envelope_hop_s
        )
        return scene.model_dump(mode="json")

    @router.post("/api/projects/{pid}/eq")
    def post_scene_eq(request: Request, pid: str, body: SceneRequest) -> Response:
        """这个场景的音色补偿 EQ（单声道 float32 WAV 冲激响应），浏览器试听与导出用同一个。"""
        fir = renderer_of(request).scene_eq(body.scene, songs_of(request).analysis(pid))
        buf = io.BytesIO()
        sf.write(buf, fir.astype("float32"), SAMPLE_RATE, format="WAV", subtype="FLOAT")
        return Response(buf.getvalue(), media_type="audio/wav")

    @router.get("/api/projects/{pid}/scene")
    def get_scene(request: Request, pid: str) -> dict:
        """用户上次保存的场景；没保存过（或旧文件已不合法）时 404，前端改用自动编排。"""
        songs_of(request).ready(pid)
        text = store.load_scene(pid)
        if text is None:
            raise ApiError(404, "NO_SAVED_SCENE", "No scene has been saved for this song")
        try:
            return Scene.model_validate_json(text).model_dump(mode="json")
        except ValidationError:
            log.warning("saved scene is invalid", extra={"event": "scene.invalid", "project_id": pid})
            raise ApiError(404, "NO_SAVED_SCENE", "The saved scene is no longer valid") from None

    @router.put("/api/projects/{pid}/scene", status_code=204)
    async def put_scene(request: Request, pid: str) -> Response:
        """整份替换保存（幂等）；先按大小上限拒绝，再按场景白名单校验。"""
        songs_of(request).ready(pid)
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

    return router
