"""场景数据模型（SPEC §4.4）：参数白名单校验、默认值、预设、规范化 JSON。"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orbit8d.engine.orbit import OrbitParams, period_seconds

TRACKS = ("vocals", "drums", "bass", "other")
TrackName = Literal["vocals", "drums", "bass", "other"]
PRESETS = ("classic", "singer", "dual", "tumble", "single")
DEFAULT_WIDTH = {"vocals": 0.0, "drums": 40.0, "bass": 0.0, "other": 40.0}
DEFAULT_SEND = {"vocals": 1.0, "drums": 0.4, "bass": 0.0, "other": 0.7}
MAX_BARS = 8
SINGLE_TURN_S = 12.0  # 参考视频实测：约 12 秒一圈
SINGLE_WET_DB = -10.0

_STRICT = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class Speed(BaseModel):
    model_config = _STRICT
    mode: Literal["bars", "seconds"] = "bars"
    bars: Literal[1, 2, 4, 8] = 4
    seconds: float = Field(7.0, ge=2.0, le=30.0)


class Orbit(BaseModel):
    model_config = _STRICT
    shape: Literal["circle", "ellipse", "pendulum", "figure8", "spiral", "fixed"] = "circle"
    radius_m: float = Field(1.2, ge=0.5, le=4.0)
    speed: Speed = Field(default_factory=Speed)
    direction: Literal["cw", "ccw"] = "cw"
    start_deg: float = Field(0.0, ge=-360.0, le=360.0)
    height_deg: float = Field(0.0, ge=-60.0, le=90.0)  # 上限 90°：可以放到顶层（正头顶）
    pitch_deg: float = Field(0.0, ge=-90.0, le=90.0)
    roll_deg: float = Field(0.0, ge=-90.0, le=90.0)
    yaw_deg: float = Field(0.0, ge=-180.0, le=180.0)
    aspect: float = Field(0.6, ge=0.3, le=1.0)
    swing_deg: float = Field(120.0, ge=10.0, le=180.0)
    lift_deg: float = Field(30.0, ge=0.0, le=60.0)


class Track(BaseModel):
    model_config = _STRICT
    orbit: Orbit = Field(default_factory=Orbit)
    gain_db: float = Field(0.0, ge=-24.0, le=12.0)
    width_deg: float = Field(0.0, ge=0.0, le=90.0)
    reverb_send: float = Field(1.0, ge=0.0, le=1.0)
    mute: bool = False
    solo: bool = False


class Room(BaseModel):
    model_config = _STRICT
    name: Literal["room", "hall", "church"] = "hall"
    wet_db: float = Field(-12.0, ge=-24.0, le=0.0)


def _default_tracks() -> dict[str, Track]:
    return {name: Track(width_deg=DEFAULT_WIDTH[name], reverb_send=DEFAULT_SEND[name]) for name in TRACKS}


class Scene(BaseModel):
    model_config = _STRICT
    version: Literal[1] = 1
    tracks: dict[TrackName, Track] = Field(default_factory=_default_tracks)
    room: Room = Field(default_factory=Room)
    rear_darken_db: float = Field(6.0, ge=0.0, le=12.0)

    @model_validator(mode="after")
    def _all_tracks_present(self) -> "Scene":
        missing = set(TRACKS) - set(self.tracks)
        if missing:
            raise ValueError(f"缺少音轨: {sorted(missing)}")
        return self


def canonical_json(scene: Scene) -> str:
    """键排序、无空白的 JSON，用于导出幂等哈希。"""
    return json.dumps(
        scene.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def effective_track_gains(scene: Scene) -> dict[str, float]:
    """用户音量 × 静音/独奏（线性）。只要有音轨独奏，其余音轨全部静音。"""
    any_solo = any(t.solo for t in scene.tracks.values())
    gains = {}
    for name, t in scene.tracks.items():
        audible = not t.mute and (t.solo or not any_solo)
        gains[name] = 10 ** (t.gain_db / 20) if audible else 0.0
    return gains


def orbit_params(orbit: Orbit, bpm_norm: float) -> OrbitParams:
    return OrbitParams(
        shape=orbit.shape,
        radius_m=orbit.radius_m,
        period_s=period_seconds(orbit.speed.mode, orbit.speed.bars, orbit.speed.seconds, bpm_norm),
        direction=1 if orbit.direction == "cw" else -1,
        start_deg=orbit.start_deg,
        height_deg=orbit.height_deg,
        pitch_deg=orbit.pitch_deg,
        roll_deg=orbit.roll_deg,
        yaw_deg=orbit.yaw_deg,
        aspect=orbit.aspect,
        swing_deg=orbit.swing_deg,
        lift_deg=orbit.lift_deg,
    )


def preset(name: str, default_bars: int) -> Scene:
    """预设：经典 8D / 歌手绕着你转 / 双环反向 / 上下翻滚 / 单点环绕（参考视频同款：整首歌一个点声源）。"""
    if name not in PRESETS:
        raise ValueError(f"未知预设: {name}")
    scene = Scene()
    for track in scene.tracks.values():
        track.orbit.speed = Speed(mode="bars", bars=default_bars)
    t = scene.tracks
    if name == "singer":
        for other in ("drums", "bass", "other"):
            t[other].orbit.shape = "fixed"
        t["drums"].width_deg, t["other"].width_deg = 60.0, 90.0
    elif name == "dual":
        t["other"].orbit.direction = "ccw"
        t["drums"].orbit.speed = Speed(mode="bars", bars=min(default_bars * 2, MAX_BARS))
    elif name == "tumble":
        t["vocals"].orbit.roll_deg = 90.0
        for anchored in ("drums", "bass"):
            t[anchored].orbit.shape = "fixed"
        t["drums"].width_deg = 60.0
    elif name == "single":
        for track in t.values():
            track.width_deg = 0.0
            track.orbit.speed = Speed(mode="seconds", seconds=SINGLE_TURN_S)
        scene.room.wet_db = SINGLE_WET_DB
    return scene
