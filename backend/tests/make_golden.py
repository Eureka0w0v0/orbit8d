"""生成跨语言一致性测试数据：uv run python -m tests.make_golden

输出到仓库根目录 shared/golden/。Python 实现是基准，TS 实现必须与之逐点一致。
改动算法后需要重新生成并在提交说明里写明原因。
contract.json 是前后端共享的枚举、布局、上限与错误码（后端常量的快照，TS 常量与 i18n 必须与之一致）：
改了这些常量同样要重新生成，并在提交说明里写明前端要跟着改什么。
"""

import ast
import json
import re
from dataclasses import fields
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from orbit8d.config import SAMPLE_RATE
from orbit8d.engine.analysis import Analysis
from orbit8d.engine.hrtf import HrtfGrid, to_bytes
from orbit8d.engine.master import LOUDNESS_TARGETS
from orbit8d.engine.mixdown import Renderer
from orbit8d.engine.orbit import BAR_CHOICES, SHAPES, OrbitParams, orbit_position
from orbit8d.engine.render import MAX_DISTANCE_M, MIN_DISTANCE_M
from orbit8d.engine.reverb import ROOMS
from orbit8d.engine.scene import (
    HOLD_RAMP_S,
    LAYER_HEIGHTS,
    MAX_EVENTS,
    MAX_LABEL,
    MAX_SECTIONS,
    TRACKS,
    Event,
    Orbit,
    Scene,
    Section,
    Speed,
)
from orbit8d.engine.sources import PREVIEW_FILES, SOURCE_CHANNELS, Sources
from orbit8d.engine.timeline import compile_track, track_position, wet_db_curve
from orbit8d.jobs.layout import ORIGINAL_PREVIEW
from orbit8d.jobs.states import ExportState, ProjectState
from orbit8d.jobs.store import ExportRecord, ProjectRecord, SourceInfo
from orbit8d.jobs.worker import STEP_MODEL_DOWNLOAD
from orbit8d.media.ffmpeg import OUTPUT_FORMATS

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "shared" / "golden"
ORBIT_FILE = GOLDEN_DIR / "orbit_vectors.json"
TIMELINE_FILE = GOLDEN_DIR / "timeline_vectors.json"
CASES_PER_SHAPE = 40
SEED = 8

# 时间轴一致性用例：随机多段场景 + 事件（外加两个构造的边界场景）→ 各轨在一批时刻的位置与混响量
TIMELINE_SCENES = 10
TIMELINE_TIMES = 40
TIMELINE_EDGE_EPS = 1e-3
TIMELINE_EVENT_TRIES = 5

# 渲染核心一致性用例：合成 HRTF 网格 + 9 路输入 → Python 渲染的干声与混响送出信号
RENDER_GRID_FILE = GOLDEN_DIR / "render_grid.bin"
RENDER_CASE_FILE = GOLDEN_DIR / "render_case.json"
RENDER_DATA_FILE = GOLDEN_DIR / "render_case.f32"
RENDER_SR = 44100
RENDER_SAMPLES = 8192
RENDER_TAPS = 48
INPUT_CHANNELS = SOURCE_CHANNELS  # vocals_hi, bass_hi, drums_hi_L/R, other_hi_L/R, bass/drums/other_sub
RENDER_BPM_NORM = 97.5
RENDER_T_REF = 0.05
RENDER_CALIBRATION = {"vocals": 1.1, "drums": 0.6, "bass": 0.9, "other": 0.7, "sub": 0.85}

# 前后端契约（web/src/contract.test.ts 对照 TS 常量、类型与 i18n 的错误提示）
CONTRACT_FILE = GOLDEN_DIR / "contract.json"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "orbit8d"
# 错误码还没有统一的登记处，散在各模块的字面量里，所以从源码收集。认得的写法（出现新写法要补进来）：
# 字典 {"code": "X", ...}，以及下表这些调用的第 n 个位置参数（字面量，或模块级的字符串常量）
CODE_ARGS = {
    "ApiError": 1,  # ApiError(413, "TOO_LARGE", message)
    "Refused": 0,  # 用例层拒绝：Refused("PROJECT_BUSY", message)
    "MediaError": 0,  # MediaError("NO_AUDIO", message)
    "super().__init__": 0,  # CodedError 的子类：super().__init__("SEPARATION_FAILED", message)
    "fail": 1,  # 任务失败：fail(job.id, "INTERNAL", message)
    "fail_project": 1,  # store.fail_project(pid, PROJECT_INTERRUPTED, message)
    "fail_export": 1,
    "_run": 2,  # ffmpeg 子进程：_run(args, timeout, "DECODE_FAILED")，超时报 <code>_TIMEOUT
}
TIMEOUT_SUFFIX = "_TIMEOUT"
CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")


def orbit_cases(seed: int = SEED) -> list[dict]:
    rng = np.random.default_rng(seed)
    cases = []
    for shape in SHAPES:
        for _ in range(CASES_PER_SHAPE):
            p = OrbitParams(
                shape=shape,
                radius_m=float(rng.uniform(0.5, 4.0)),
                period_s=float(rng.uniform(2.0, 30.0)),
                direction=int(rng.choice([1, -1])),
                start_deg=float(rng.uniform(-360, 360)),
                height_deg=float(rng.uniform(-60, 90)),
                pitch_deg=float(rng.uniform(-90, 90)),
                roll_deg=float(rng.uniform(-90, 90)),
                yaw_deg=float(rng.uniform(-180, 180)),
                aspect=float(rng.uniform(0.3, 1.0)),
                swing_deg=float(rng.uniform(10, 180)),
                lift_deg=float(rng.uniform(0, 60)),
            )
            t = rng.uniform(0, 300, size=3)
            t_ref = float(rng.uniform(0, 10))
            offset = float(rng.choice([0.0, -20.0, 20.0, -45.0]))
            az, el, dist = orbit_position(p, t, t_ref, offset)
            cases.append(
                {
                    "params": p.__dict__,
                    "t": t.tolist(),
                    "t_ref": t_ref,
                    "offset_deg": offset,
                    "az": az.tolist(),
                    "el": el.tolist(),
                    "dist": dist.tolist(),
                }
            )
    return cases


def render_grid(rng: np.random.Generator) -> HrtfGrid:
    decay = np.exp(-np.arange(RENDER_TAPS) / 10.0)
    data = (rng.standard_normal((5, 12, 2, RENDER_TAPS)) * decay).astype(np.float32)
    return HrtfGrid(
        sample_rate=RENDER_SR, az_step_deg=30.0, el_nodes=np.array([-60.0, -30.0, 0.0, 30.0, 60.0]), data=data
    )


def random_orbit(rng: np.random.Generator) -> Orbit:
    mode = str(rng.choice(["bars", "seconds"]))
    return Orbit(
        shape=str(rng.choice(SHAPES)),
        radius_m=float(rng.uniform(0.5, 4.0)),
        speed=Speed(mode=mode, bars=int(rng.choice([1, 2, 4, 8])), seconds=float(rng.uniform(2.0, 30.0))),
        direction=str(rng.choice(["cw", "ccw"])),
        start_deg=float(rng.uniform(-360, 360)),
        height_deg=float(rng.uniform(-60, 90)),
        pitch_deg=float(rng.uniform(-90, 90)),
        roll_deg=float(rng.uniform(-90, 90)),
        yaw_deg=float(rng.uniform(-180, 180)),
        aspect=float(rng.uniform(0.3, 1.0)),
        swing_deg=float(rng.uniform(10, 180)),
        lift_deg=float(rng.uniform(0, 60)),
    )


def random_timeline_scene(rng: np.random.Generator, duration: float) -> Scene:
    count = int(rng.integers(1, 6))
    starts = [0.0, *sorted(rng.uniform(1.0, duration - 1.0, size=count - 1).tolist())]
    sections = [
        Section(
            start_s=start,
            label=f"段{k + 1}",
            orbits={track: random_orbit(rng) for track in TRACKS},
            wet_db=float(rng.uniform(-24.0, 0.0)),
        )
        for k, start in enumerate(starts)
    ]
    scene = Scene(sections=sections)
    events: list[Event] = []
    for _ in range(TIMELINE_EVENT_TRIES):  # 随机撒事件，与已有事件冲突的丢掉
        targets = rng.choice(TRACKS, size=int(rng.integers(1, 5)), replace=False).tolist()
        candidate = Event(
            t_s=float(rng.uniform(0.0, duration - 10.0)),
            kind=str(rng.choice(["hold", "overhead"])),
            duration_s=float(rng.uniform(0.5, 8.0)),
            targets=targets,
        )
        try:
            scene = Scene.model_validate({**scene.model_dump(), "events": [*events, candidate]})
            events.append(candidate)
        except ValidationError:
            continue
    return scene


def opposite_fixed_scene() -> Scene:
    """构造的边界：正前固定 → 正后固定（方向正好相反），外加一个中点停在头顶的事件。"""
    first = Section(orbits={track: Orbit(shape="fixed") for track in TRACKS})
    second = Section(
        start_s=20.0, label="反向", orbits={track: Orbit(shape="fixed", start_deg=180.0) for track in TRACKS}
    )
    return Scene(
        sections=[first, second],
        events=[Event(t_s=40.0, kind="overhead", duration_s=4.0, targets=["vocals"])],
    )


def sample_times(rng: np.random.Generator, scene: Scene, duration: float) -> np.ndarray:
    """随机时刻 + 分界、事件起止附近的时刻。"""
    marks = [s.start_s for s in scene.sections[1:]]
    for e in scene.events:
        lo, hi = e.span()
        marks += [lo, (lo + hi) / 2, hi]
    edges = [m + d for m in marks for d in (-TIMELINE_EDGE_EPS, 0.0, TIMELINE_EDGE_EPS)]
    t = np.concatenate([rng.uniform(0.0, duration, size=TIMELINE_TIMES), np.clip(edges, 0.0, None)])
    return np.sort(t)


def timeline_cases(seed: int = SEED) -> list[dict]:
    rng = np.random.default_rng(seed + 1)
    cases = []
    scenes = [(float(rng.uniform(60, 300)), None) for _ in range(TIMELINE_SCENES)] + [
        (60.0, opposite_fixed_scene())
    ]
    for duration, fixed in scenes:
        scene = fixed or random_timeline_scene(rng, duration)
        bpm_norm, t_ref = float(rng.uniform(70, 140)), float(rng.uniform(0, 5))
        t = sample_times(rng, scene, duration)
        tracks = {}
        for track in TRACKS:
            offset = float(rng.choice([0.0, -20.0, 20.0, -45.0]))
            az, el, dist = track_position(compile_track(scene, track, bpm_norm, t_ref, duration), t, offset)
            tracks[track] = {
                "offset_deg": offset,
                "az": az.tolist(),
                "el": el.tolist(),
                "dist": dist.tolist(),
            }
        cases.append(
            {
                "scene": scene.model_dump(mode="json"),
                "bpm_norm": bpm_norm,
                "t_ref": t_ref,
                "duration_s": duration,
                "t": t.tolist(),
                "tracks": tracks,
                "wet_db": wet_db_curve(scene, bpm_norm, duration, t).tolist(),
            }
        )
    return cases


def render_scene() -> Scene:
    """覆盖所有代码路径：三段（含过渡、固定声源段）+ 停顿 + 飞过头顶 + 分段混响量；
    快速转动 + 仰角 + 倾斜、逆时针椭圆、偏航 8 字、螺旋、钟摆、固定声源、音量、背后压暗。
    渲染只有 0.19 秒，所以分段与事件都压在这段时间里。"""
    first = Section(
        orbits={
            "vocals": Orbit(speed=Speed(mode="seconds", seconds=2.0), height_deg=20.0, pitch_deg=15.0),
            "drums": Orbit(shape="ellipse", aspect=0.5, radius_m=0.7, direction="ccw"),
            "other": Orbit(
                shape="figure8",
                swing_deg=100.0,
                lift_deg=40.0,
                yaw_deg=30.0,
                speed=Speed(mode="bars", bars=1),
            ),
            "bass": Orbit(shape="fixed", start_deg=45.0, height_deg=-10.0, radius_m=2.0),
        },
        wet_db=-12.0,
    )
    second = Section(
        start_s=0.06,
        label="副歌",
        orbits={
            "vocals": Orbit(
                pitch_deg=60.0,
                yaw_deg=-30.0,
                direction="ccw",
                radius_m=0.8,
                speed=Speed(mode="seconds", seconds=4.0),
            ),
            "drums": Orbit(
                shape="spiral", lift_deg=50.0, height_deg=10.0, speed=Speed(mode="seconds", seconds=2.0)
            ),
            "other": Orbit(roll_deg=45.0, speed=Speed(mode="bars", bars=1)),
            "bass": Orbit(shape="fixed", start_deg=135.0, radius_m=1.5),
        },
        wet_db=-4.0,
    )
    third = Section(
        start_s=0.12,
        label="尾声",
        orbits={
            "vocals": Orbit(shape="fixed", start_deg=-60.0, height_deg=30.0),
            "drums": Orbit(shape="pendulum", swing_deg=150.0, speed=Speed(mode="seconds", seconds=2.0)),
            "other": Orbit(shape="ellipse", aspect=0.4, speed=Speed(mode="seconds", seconds=3.0)),
            "bass": Orbit(speed=Speed(mode="seconds", seconds=2.0)),
        },
        wet_db=-20.0,
    )
    s = Scene(
        sections=[first, second, third],
        events=[
            Event(t_s=0.02, kind="hold", duration_s=0.5, targets=["drums"]),
            Event(t_s=0.03, kind="overhead", duration_s=0.5, targets=["vocals", "other"]),
        ],
        rear_darken_db=9.0,
    )
    s.mix["drums"].width_deg, s.mix["drums"].gain_db = 50.0, -3.0
    s.mix["other"].width_deg = 30.0
    return s


def render_case(seed: int = SEED) -> tuple[dict, np.ndarray, HrtfGrid]:
    rng = np.random.default_rng(seed)
    grid = render_grid(rng)
    inputs = (
        (rng.standard_normal((len(INPUT_CHANNELS), RENDER_SAMPLES)) * 0.1)
        .astype(np.float32)
        .astype(np.float64)
    )
    src = Sources(data=inputs.astype(np.float32), sr=RENDER_SR)  # 9 路的顺序与 INPUT_CHANNELS 相同
    scene = render_scene()
    renderer = Renderer(grid)
    tracks = renderer.render_tracks(src, scene, RENDER_BPM_NORM, RENDER_T_REF, RENDER_CALIBRATION)
    dry = sum(tracks.values())
    send = renderer.send_signal(src, scene, RENDER_CALIBRATION, RENDER_BPM_NORM)
    case = {
        "version": 2,
        "sample_rate": RENDER_SR,
        "samples": RENDER_SAMPLES,
        "channels": list(INPUT_CHANNELS),
        "scene": scene.model_dump(mode="json"),
        "bpm_norm": RENDER_BPM_NORM,
        "t_ref": RENDER_T_REF,
        "calibration": RENDER_CALIBRATION,
        "layout": "float32 LE: inputs[channel][sample], dry_L, dry_R, send",
    }
    data = np.concatenate([inputs.ravel(), dry[:, 0], dry[:, 1], send]).astype("<f4")
    return case, data, grid


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if not isinstance(func, ast.Attribute):
        return ""
    inner = func.value
    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "super":
        return f"super().{func.attr}"
    return func.attr


def _module_constants(tree: ast.Module) -> dict[str, object]:
    """模块级的 NAME = 常量（错误码有时先放进常量，如 store.PROJECT_INTERRUPTED）。"""
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _code(node: ast.expr, constants: dict[str, object]) -> str | None:
    value = node.value if isinstance(node, ast.Constant) else constants.get(getattr(node, "id", ""))
    return value if isinstance(value, str) and CODE_PATTERN.fullmatch(value) else None


def _codes_at(node: ast.AST, constants: dict[str, object]) -> set[str]:
    """一个语法节点上写着的错误码（CODE_ARGS 说明了认得哪些写法）。"""
    if isinstance(node, ast.Dict):
        pairs = zip(node.keys, node.values, strict=True)
        values = [v for k, v in pairs if isinstance(k, ast.Constant) and k.value == "code"]
        return {c for v in values if (c := _code(v, constants))}
    if not isinstance(node, ast.Call):
        return set()
    name = _call_name(node)
    pos = CODE_ARGS.get(name)
    if pos is None or pos >= len(node.args) or not (code := _code(node.args[pos], constants)):
        return set()
    return {code, code + TIMEOUT_SUFFIX} if name == "_run" else {code}


def error_codes() -> list[str]:
    """后端会返回、或写进项目 / 导出记录的全部错误码，排好序。"""
    codes: set[str] = set()
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            codes |= _codes_at(node, constants)
    return sorted(codes)


def contract() -> dict:
    """前后端共享的枚举、布局、上限与错误码。scene_schema 就是 GET /api/scene/schema 返回的那一份。"""
    return {
        "version": 1,
        "tracks": list(TRACKS),
        "shapes": list(SHAPES),
        "rooms": list(ROOMS),
        "bar_choices": list(BAR_CHOICES),
        "export_formats": list(OUTPUT_FORMATS),
        "loudness": list(LOUDNESS_TARGETS),
        "project_states": [str(s) for s in ProjectState],
        "export_states": [str(s) for s in ExportState],
        "project_steps": [STEP_MODEL_DOWNLOAD],  # 项目记录的 step；空字符串 = 没有子步骤
        "source_channels": list(SOURCE_CHANNELS),
        "preview_files": {name: list(channels) for name, channels in PREVIEW_FILES.items()},
        "original_preview": ORIGINAL_PREVIEW,
        "sample_rate": SAMPLE_RATE,
        "hold_ramp_s": HOLD_RAMP_S,
        "layer_heights": LAYER_HEIGHTS,
        "limits": {
            "max_sections": MAX_SECTIONS,
            "max_events": MAX_EVENTS,
            "max_label": MAX_LABEL,
            "distance_m": [MIN_DISTANCE_M, MAX_DISTANCE_M],  # 渲染时距离增益的钳位
        },
        "error_codes": error_codes(),
        # 接口返回的记录各有哪些字段（GET /api/projects/{id}、/api/exports/{id}）；前端读的字段必须都在里面
        "record_fields": {
            "project": [f.name for f in fields(ProjectRecord)],
            "source": [f.name for f in fields(SourceInfo)],
            "analysis": [f.name for f in fields(Analysis)],
            "export": [f.name for f in fields(ExportRecord)],
        },
        "scene_schema": Scene.model_json_schema(),
    }


def contract_json() -> str:
    return json.dumps(contract(), indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    ORBIT_FILE.write_text(json.dumps({"version": 1, "cases": orbit_cases()}, indent=1) + "\n")
    TIMELINE_FILE.write_text(json.dumps({"version": 1, "cases": timeline_cases()}) + "\n")
    case, data, grid = render_case()
    RENDER_CASE_FILE.write_text(json.dumps(case, indent=1) + "\n")
    RENDER_DATA_FILE.write_bytes(data.tobytes())
    RENDER_GRID_FILE.write_bytes(to_bytes(grid))
    CONTRACT_FILE.write_text(contract_json(), encoding="utf-8")
    print(f"wrote {ORBIT_FILE}, {TIMELINE_FILE}, {RENDER_CASE_FILE}, {RENDER_DATA_FILE}, {RENDER_GRID_FILE}")
    print(f"wrote {CONTRACT_FILE}")


if __name__ == "__main__":
    main()
