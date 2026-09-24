"""场景数据模型 v2（SPEC §4.4、§13）：全曲混音 + 分段轨道 + 事件；
白名单校验、v1 自动升级、预设、规范化 JSON。"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orbit8d.engine.orbit import OrbitParams, period_seconds

TRACKS = ("vocals", "drums", "bass", "other")
TrackName = Literal["vocals", "drums", "bass", "other"]
PRESETS = ("classic", "singer", "dual", "tumble", "layers", "diagonal", "cross")
DEFAULT_WIDTH = {"vocals": 0.0, "drums": 40.0, "bass": 0.0, "other": 40.0}
DEFAULT_SEND = {"vocals": 1.0, "drums": 0.4, "bass": 0.0, "other": 0.7}
DEFAULT_WET_DB = -12.0
DEFAULT_LABEL = "全曲"
MAX_BARS = 8
MAX_SONG_S = 3600.0
MAX_SECTIONS = 16
MAX_EVENTS = 64
MAX_LABEL = 16
HOLD_RAMP_S = 0.5  # 停顿前后各 0.5 秒减速 / 加速（timeline.py 同一常量）
LAYER_HEIGHTS = {"surround": 0.0, "height": 35.0, "top": 75.0}  # 与前端 layers.ts 的一键高度一致
DIAGONAL_TILT = 45.0
VERTICAL = 90.0

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


class Mix(BaseModel):
    """全曲共用的混音设置（不随分段变化）。"""

    model_config = _STRICT
    gain_db: float = Field(0.0, ge=-24.0, le=12.0)
    width_deg: float = Field(0.0, ge=0.0, le=90.0)
    reverb_send: float = Field(1.0, ge=0.0, le=1.0)
    mute: bool = False
    solo: bool = False


def _default_orbits() -> dict[str, Orbit]:
    return {name: Orbit() for name in TRACKS}


def _default_mix() -> dict[str, Mix]:
    return {name: Mix(width_deg=DEFAULT_WIDTH[name], reverb_send=DEFAULT_SEND[name]) for name in TRACKS}


def _require_all_tracks(names, what: str) -> None:
    missing = set(TRACKS) - set(names)
    if missing:
        raise ValueError(f"{what}缺少音轨: {sorted(missing)}")


class Section(BaseModel):
    model_config = _STRICT
    start_s: float = Field(0.0, ge=0.0, le=MAX_SONG_S)
    label: str = Field(DEFAULT_LABEL, min_length=1, max_length=MAX_LABEL)
    orbits: dict[TrackName, Orbit] = Field(default_factory=_default_orbits)
    wet_db: float = Field(DEFAULT_WET_DB, ge=-24.0, le=0.0)

    @model_validator(mode="after")
    def _complete(self) -> "Section":
        _require_all_tracks(self.orbits, "分段")
        return self


class Event(BaseModel):
    model_config = _STRICT
    t_s: float = Field(ge=0.0, le=MAX_SONG_S)
    kind: Literal["hold", "overhead"]
    duration_s: float = Field(ge=0.5, le=30.0)
    targets: list[TrackName] = Field(min_length=1, max_length=len(TRACKS))

    @model_validator(mode="after")
    def _unique_targets(self) -> "Event":
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("事件的目标音轨重复")
        return self

    def span(self) -> tuple[float, float]:
        """事件实际影响的时间段（停顿含前后减速 / 加速）。"""
        ramp = HOLD_RAMP_S if self.kind == "hold" else 0.0
        return self.t_s, self.t_s + self.duration_s + 2 * ramp


class Room(BaseModel):
    model_config = _STRICT
    name: Literal["room", "hall", "church"] = "hall"


def upgrade_v1(data: dict[str, Any]) -> dict[str, Any]:
    """v1 场景（tracks 里同时放混音和轨道、room 里放混响量）→ v2（只有一段“全曲”）。"""
    tracks = data.get("tracks", {})
    room = data.get("room", {})
    return {
        "version": 2,
        "mix": {name: {k: v for k, v in t.items() if k != "orbit"} for name, t in tracks.items()},
        "sections": [
            {
                "start_s": 0.0,
                "label": DEFAULT_LABEL,
                "orbits": {name: t.get("orbit", {}) for name, t in tracks.items()},
                "wet_db": room.get("wet_db", DEFAULT_WET_DB),
            }
        ],
        "events": [],
        "room": {"name": room.get("name", "hall")},
        "rear_darken_db": data.get("rear_darken_db", 6.0),
    }


class Scene(BaseModel):
    model_config = _STRICT
    version: Literal[2] = 2
    mix: dict[TrackName, Mix] = Field(default_factory=_default_mix)
    sections: list[Section] = Field(
        default_factory=lambda: [Section()], min_length=1, max_length=MAX_SECTIONS
    )
    events: list[Event] = Field(default_factory=list, max_length=MAX_EVENTS)
    room: Room = Field(default_factory=Room)
    rear_darken_db: float = Field(6.0, ge=0.0, le=12.0)

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("version") == 1:
            return upgrade_v1(data)
        return data

    @model_validator(mode="after")
    def _consistent(self) -> "Scene":
        _require_all_tracks(self.mix, "混音")
        if self.sections[0].start_s != 0.0:
            raise ValueError("第一段必须从 0 秒开始")
        starts = [s.start_s for s in self.sections]
        if any(b <= a for a, b in zip(starts, starts[1:], strict=False)):
            raise ValueError("分段开始时间必须严格递增")
        spans: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for e in self.events:
            for track in e.targets:
                spans.setdefault((e.kind, track), []).append(e.span())
        for (kind, track), items in spans.items():
            items.sort()
            if any(b[0] < a[1] for a, b in zip(items, items[1:], strict=False)):
                raise ValueError(f"{track} 的 {kind} 事件时间重叠")
        return self


def canonical_json(scene: Scene) -> str:
    """键排序、无空白的 JSON，用于导出幂等哈希。"""
    return json.dumps(
        scene.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def effective_track_gains(scene: Scene) -> dict[str, float]:
    """用户音量 × 静音/独奏（线性）。只要有音轨独奏，其余音轨全部静音。"""
    any_solo = any(m.solo for m in scene.mix.values())
    gains = {}
    for name, m in scene.mix.items():
        audible = not m.mute and (m.solo or not any_solo)
        gains[name] = 10 ** (m.gain_db / 20) if audible else 0.0
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


def preset_parts(name: str, default_bars: int) -> tuple[dict[str, Orbit], dict[str, Mix], float]:
    """预设拆成三部分：各轨轨道、全曲混音、这一段的混响量。自动编排按段复用轨道部分。

    倾斜统一用“前后倾斜 + 水平转向”表示：前后倾斜 = 翘起的角度，水平转向 = 翘起的方向。
    """
    if name not in PRESETS:
        raise ValueError(f"未知预设: {name}")
    o = {track: Orbit(speed=Speed(mode="bars", bars=default_bars)) for track in TRACKS}
    mix = _default_mix()
    wet = DEFAULT_WET_DB
    if name == "singer":
        for other in ("drums", "bass", "other"):
            o[other].shape = "fixed"
        mix["drums"].width_deg, mix["other"].width_deg = 60.0, 90.0
    elif name == "dual":
        o["other"].direction = "ccw"
        o["drums"].speed = Speed(mode="bars", bars=min(default_bars * 2, MAX_BARS))
    elif name == "tumble":
        o["vocals"].roll_deg = 90.0
        for anchored in ("drums", "bass"):
            o[anchored].shape = "fixed"
        mix["drums"].width_deg = 60.0
    elif name == "layers":
        o["vocals"].height_deg = LAYER_HEIGHTS["top"]
        o["other"].height_deg = LAYER_HEIGHTS["height"]
        o["other"].direction = "ccw"
        o["drums"].height_deg = LAYER_HEIGHTS["surround"]
        o["bass"].shape = "fixed"
    elif name == "diagonal":
        o["vocals"].pitch_deg, o["vocals"].yaw_deg = DIAGONAL_TILT, 45.0
        o["other"].pitch_deg, o["other"].yaw_deg = DIAGONAL_TILT, -45.0
        o["other"].direction = "ccw"
        o["bass"].shape = "fixed"
    elif name == "cross":
        o["vocals"].pitch_deg = VERTICAL  # 左耳 → 头顶 → 右耳
        o["other"].pitch_deg, o["other"].yaw_deg = VERTICAL, 90.0  # 正前 → 头顶 → 脑后
        o["bass"].shape = "fixed"
    return o, mix, wet


def preset(name: str, default_bars: int) -> Scene:
    """预设：经典 8D / 歌手绕着你转 / 双环反向 / 上下翻滚 / 三层环绕 / 斜向环绕 / 立体交叉。
    都是只有一段“全曲”的场景，速度都跟随歌曲小节。"""
    orbits, mix, wet = preset_parts(name, default_bars)
    return Scene(mix=mix, sections=[Section(orbits=orbits, wet_db=wet)])
