"""生成跨语言一致性测试数据：uv run python -m tests.make_golden

输出到仓库根目录 shared/golden/。Python 实现是基准，TS 实现必须与之逐点一致。
改动算法后需要重新生成并在提交说明里写明原因。
"""

import json
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from orbit8d.engine.hrtf import HrtfGrid, to_bytes
from orbit8d.engine.orbit import SHAPES, OrbitParams, orbit_position
from orbit8d.engine.pipeline import Renderer, Sources
from orbit8d.engine.scene import TRACKS, Event, Orbit, Scene, Section, Speed
from orbit8d.engine.timeline import compile_track, track_position, wet_db_curve

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
INPUT_CHANNELS = (
    "vocals_hi", "bass_hi", "drums_hi_L", "drums_hi_R", "other_hi_L", "other_hi_R",
    "bass_sub", "drums_sub", "other_sub",
)  # fmt: skip
RENDER_BPM_NORM = 97.5
RENDER_T_REF = 0.05
RENDER_CALIBRATION = {"vocals": 1.1, "drums": 0.6, "bass": 0.9, "other": 0.7, "sub": 0.85}


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
    src = Sources(
        hi={"vocals": inputs[0], "bass": inputs[1], "drums": inputs[2:4].T, "other": inputs[4:6].T},
        sub={"bass": inputs[6], "drums": inputs[7], "other": inputs[8]},
        energy_hi={"vocals": 1.0, "bass": 1.0, "drums": 1.0, "other": 1.0},
        energy_sub=1.0,
        sr=RENDER_SR,
    )
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


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    ORBIT_FILE.write_text(json.dumps({"version": 1, "cases": orbit_cases()}, indent=1) + "\n")
    TIMELINE_FILE.write_text(json.dumps({"version": 1, "cases": timeline_cases()}) + "\n")
    case, data, grid = render_case()
    RENDER_CASE_FILE.write_text(json.dumps(case, indent=1) + "\n")
    RENDER_DATA_FILE.write_bytes(data.tobytes())
    RENDER_GRID_FILE.write_bytes(to_bytes(grid))
    print(f"wrote {ORBIT_FILE}, {TIMELINE_FILE}, {RENDER_CASE_FILE}, {RENDER_DATA_FILE}, {RENDER_GRID_FILE}")


if __name__ == "__main__":
    main()
