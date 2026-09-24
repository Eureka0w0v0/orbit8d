"""生成跨语言一致性测试数据：uv run python -m tests.make_golden

输出到仓库根目录 shared/golden/。Python 实现是基准，TS 实现必须与之逐点一致。
改动算法后需要重新生成并在提交说明里写明原因。
"""

import json
from pathlib import Path

import numpy as np

from orbit8d.engine.hrtf import HrtfGrid, to_bytes
from orbit8d.engine.orbit import SHAPES, OrbitParams, orbit_position
from orbit8d.engine.pipeline import Renderer, Sources
from orbit8d.engine.scene import Scene, Speed

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "shared" / "golden"
ORBIT_FILE = GOLDEN_DIR / "orbit_vectors.json"
CASES_PER_SHAPE = 40
SEED = 8

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
                height_deg=float(rng.uniform(-60, 60)),
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


def render_scene() -> Scene:
    """覆盖所有代码路径：快速转动 + 仰角 + 倾斜、逆时针椭圆、偏航 8 字、固定声源、音量、背后压暗。"""
    s = Scene()
    v, d, o, b = (s.tracks[k] for k in ("vocals", "drums", "other", "bass"))
    v.orbit.speed = Speed(mode="seconds", seconds=2.0)
    v.orbit.height_deg, v.orbit.pitch_deg = 20.0, 15.0
    d.orbit.shape, d.orbit.aspect, d.orbit.radius_m, d.orbit.direction = "ellipse", 0.5, 0.7, "ccw"
    d.width_deg, d.gain_db = 50.0, -3.0
    o.orbit.shape, o.orbit.swing_deg, o.orbit.lift_deg, o.orbit.yaw_deg = "figure8", 100.0, 40.0, 30.0
    o.orbit.speed = Speed(mode="bars", bars=1)
    o.width_deg = 30.0
    b.orbit.shape, b.orbit.start_deg, b.orbit.height_deg, b.orbit.radius_m = "fixed", 45.0, -10.0, 2.0
    s.rear_darken_db = 9.0
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
    send = renderer.send_signal(src, scene, RENDER_CALIBRATION)
    case = {
        "version": 1,
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
    case, data, grid = render_case()
    RENDER_CASE_FILE.write_text(json.dumps(case, indent=1) + "\n")
    RENDER_DATA_FILE.write_bytes(data.tobytes())
    RENDER_GRID_FILE.write_bytes(to_bytes(grid))
    print(f"wrote {ORBIT_FILE}, {RENDER_CASE_FILE}, {RENDER_DATA_FILE}, {RENDER_GRID_FILE}")


if __name__ == "__main__":
    main()
