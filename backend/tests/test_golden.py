"""回归护栏：Python 实现必须仍与已提交的黄金数据一致（TS 端用同一份数据校验）。"""

import json

import numpy as np

from orbit8d.engine.orbit import OrbitParams, orbit_position
from orbit8d.engine.scene import TRACKS, Scene
from orbit8d.engine.timeline import compile_track, track_position, wet_db_curve
from tests.make_golden import ORBIT_FILE, RENDER_DATA_FILE, TIMELINE_FILE, render_case


def unit(az, el):
    a, e = np.radians(az), np.radians(el)
    return np.stack([np.cos(e) * np.sin(a), np.sin(e), np.cos(e) * np.cos(a)], -1)


def test_orbit_matches_committed_golden_vectors():
    assert ORBIT_FILE.exists(), "缺少黄金数据：运行 uv run python -m tests.make_golden"
    data = json.loads(ORBIT_FILE.read_text())
    assert data["version"] == 1 and len(data["cases"]) >= 200
    worst = 0.0
    for case in data["cases"]:
        az, el, dist = orbit_position(
            OrbitParams(**case["params"]), np.array(case["t"]), case["t_ref"], case["offset_deg"]
        )
        d_az = np.abs((az - np.array(case["az"]) + 180.0) % 360.0 - 180.0)
        worst = max(
            worst,
            d_az.max(),
            np.abs(el - np.array(case["el"])).max(),
            np.abs(dist - np.array(case["dist"])).max(),
        )
    assert worst < 1e-9


def test_timeline_matches_committed_golden_vectors():
    """方向按单位向量比较（头顶附近方位角没有意义）。"""
    assert TIMELINE_FILE.exists(), "缺少黄金数据：运行 uv run python -m tests.make_golden"
    data = json.loads(TIMELINE_FILE.read_text())
    assert data["version"] == 1 and len(data["cases"]) >= 10
    worst_dir = worst_dist = worst_wet = 0.0
    for case in data["cases"]:
        scene, t = Scene.model_validate(case["scene"]), np.array(case["t"])
        for track in TRACKS:
            ref = case["tracks"][track]
            m = compile_track(scene, track, case["bpm_norm"], case["t_ref"], case["duration_s"])
            az, el, dist = track_position(m, t, ref["offset_deg"])
            worst_dir = max(
                worst_dir, np.abs(unit(az, el) - unit(np.array(ref["az"]), np.array(ref["el"]))).max()
            )
            worst_dist = max(worst_dist, np.abs(dist - np.array(ref["dist"])).max())
        wet = wet_db_curve(scene, case["bpm_norm"], case["duration_s"], t)
        worst_wet = max(worst_wet, np.abs(wet - np.array(case["wet_db"])).max())
    assert worst_dir < 1e-9 and worst_dist < 1e-9 and worst_wet < 1e-9


def test_render_matches_committed_golden_case():
    assert RENDER_DATA_FILE.exists(), "缺少黄金数据：运行 uv run python -m tests.make_golden"
    _, fresh, _ = render_case()
    committed = np.frombuffer(RENDER_DATA_FILE.read_bytes(), dtype="<f4")
    assert fresh.shape == committed.shape
    assert np.abs(fresh - committed).max() < 1e-6
