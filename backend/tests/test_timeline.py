"""时间轴运动（SPEC §13.3）：相位连续、段落过渡、停顿、飞过头顶、分段混响量。"""

import numpy as np
import pytest

from orbit8d.engine.orbit import orbit_position
from orbit8d.engine.scene import Event, Scene, orbit_params, preset
from orbit8d.engine.timeline import compile_track, track_position, wet_db_curve

BPM = 90.0
BAR = 4 * 60 / BPM
T_REF = 1.3
DUR = 120.0


def ang(a_az, a_el, b_az, b_el):
    """两个方向之间的夹角（度）。"""
    a = np.radians([a_az, a_el]) if np.isscalar(a_az) else (np.radians(a_az), np.radians(a_el))
    b = (np.radians(b_az), np.radians(b_el))
    va = np.stack([np.cos(a[1]) * np.sin(a[0]), np.sin(a[1]), np.cos(a[1]) * np.cos(a[0])], -1)
    vb = np.stack([np.cos(b[1]) * np.sin(b[0]), np.sin(b[1]), np.cos(b[1]) * np.cos(b[0])], -1)
    cross = np.linalg.norm(np.cross(va, vb), axis=-1)  # atan2 形式：小角度下数值稳定（arccos 会放大舍入误差）
    return np.degrees(np.arctan2(cross, (va * vb).sum(-1)))


def two_section_scene() -> Scene:
    a = preset("classic", 4).sections[0]
    b = (
        preset("diagonal", 1)
        .sections[0]
        .model_copy(update={"start_s": 40.0, "label": "副歌", "wet_db": -6.0})
    )
    return Scene(sections=[a, b])


@pytest.mark.parametrize("name", ["classic", "diagonal", "cross", "layers", "singer", "single"])
@pytest.mark.parametrize("offset", [0.0, -20.0])
def test_single_section_without_events_equals_v1(name, offset):
    scene = preset(name, 2)
    t = np.linspace(0, DUR, 2001)
    for track in ("vocals", "drums", "bass", "other"):
        m = compile_track(scene, track, BPM, T_REF, DUR)
        az, el, dist = track_position(m, t, offset)
        ref = orbit_position(orbit_params(scene.sections[0].orbits[track], BPM), t, T_REF, offset)
        assert ang(az, el, ref[0], ref[1]).max() < 1e-7 and np.abs(dist - ref[2]).max() < 1e-12


def test_motion_is_continuous_across_sections_and_speed_changes():
    scene = two_section_scene()
    t = np.arange(0, DUR, 0.001)
    m = compile_track(scene, "vocals", BPM, T_REF, DUR)
    az, el, _ = track_position(m, t, 0.0)
    step = ang(az[:-1], el[:-1], az[1:], el[1:])
    assert step.max() < 0.5  # 每毫秒转动不超过 0.5°：没有跳变
    # 过渡结束后按新段的速度走（1 小节一圈 = 360°/BAR）
    late = (t > 40 + BAR) & (t < 60)
    assert np.median(step[late[:-1]]) == pytest.approx(360 / BAR * 0.001, rel=0.05)


def test_hold_stops_then_resumes():
    scene = preset("classic", 2)
    scene.events = [Event(t_s=30.0, kind="hold", duration_s=3.0, targets=["vocals"])]
    m = compile_track(scene, "vocals", BPM, T_REF, DUR)
    t = np.arange(0, DUR, 0.001)
    az, el, _ = track_position(m, t, 0.0)
    step = ang(az[:-1], el[:-1], az[1:], el[1:])
    still = (t[:-1] > 30.5 + 1e-3) & (t[:-1] < 33.5 - 1e-3)
    assert step[still].max() < 1e-9  # 停住的 3 秒完全不动
    assert step.max() < 0.5  # 减速、加速平滑
    after = (t[:-1] > 35) & (t[:-1] < 40)
    assert np.median(step[after]) == pytest.approx(360 / (2 * BAR) * 0.001, rel=0.05)
    # 其他音轨不受影响
    other = compile_track(scene, "drums", BPM, T_REF, DUR)
    ref = orbit_position(orbit_params(scene.sections[0].orbits["drums"], BPM), t, T_REF, 20.0)
    got = track_position(other, t, 20.0)
    assert ang(got[0], got[1], ref[0], ref[1]).max() < 1e-7


def test_overhead_reaches_zenith_at_midpoint_and_blends_at_edges():
    scene = preset("classic", 2)
    scene.events = [Event(t_s=50.0, kind="overhead", duration_s=4.0, targets=["vocals", "other"])]
    m = compile_track(scene, "vocals", BPM, T_REF, DUR)
    _, el_mid, _ = track_position(m, np.array([52.0]), 0.0)
    assert el_mid[0] == pytest.approx(90.0, abs=1e-6)
    t = np.array([49.999, 50.0, 54.0, 54.001])
    az, el, _ = track_position(m, t, 0.0)
    ref = orbit_position(orbit_params(scene.sections[0].orbits["vocals"], BPM), t, T_REF, 0.0)
    assert ang(az, el, ref[0], ref[1]).max() < 1e-6
    t = np.arange(49, 55, 0.001)
    az, el, _ = track_position(m, t, 0.0)
    assert ang(az[:-1], el[:-1], az[1:], el[1:]).max() < 0.5


def test_fixed_section_is_not_moved_by_accumulated_phase():
    first = preset("classic", 1).sections[0]
    fixed = preset("classic", 1).sections[0].model_copy(update={"start_s": 30.0})
    fixed.orbits["vocals"].shape = "fixed"
    fixed.orbits["vocals"].start_deg = 45.0
    m = compile_track(Scene(sections=[first, fixed]), "vocals", BPM, T_REF, DUR)
    az, el, _ = track_position(m, np.array([40.0, 70.0, 100.0]), 10.0)
    assert np.allclose(az, 55.0) and np.allclose(el, 0.0, atol=1e-9)


def test_opposite_directions_blend_without_nan():
    a = preset("singer", 2).sections[0]  # 贝斯固定在正前
    b = a.model_copy(update={"start_s": 20.0}, deep=True)
    b.orbits["bass"].start_deg = 180.0  # 下一段固定在正后：两段方向正好相反
    m = compile_track(Scene(sections=[a, b]), "bass", BPM, T_REF, DUR)
    t = np.arange(15, 25, 0.001)
    az, el, _ = track_position(m, t, 0.0)
    assert np.isfinite(az).all() and np.isfinite(el).all()
    assert ang(az[:-1], el[:-1], az[1:], el[1:]).max() < 0.5


def test_wet_curve_follows_sections_smoothly():
    scene = two_section_scene()
    t = np.arange(0, DUR, 0.01)
    wet = wet_db_curve(scene, BPM, DUR, t)
    assert wet[t < 38].max() == pytest.approx(-12.0) and wet[t > 42].min() == pytest.approx(-6.0)
    assert np.all(np.diff(wet) >= -1e-12)  # 从 -12 平滑升到 -6，不回头
    single = wet_db_curve(preset("classic", 2), BPM, DUR, t)
    assert np.allclose(single, -12.0)
