"""轨道公式：用几何上显然的点做断言（SPEC §4）。"""

import numpy as np
import pytest

from orbit8d.engine.orbit import OrbitParams, normalize_bpm, orbit_position, period_seconds

T = 8.0


def params(**kw) -> OrbitParams:
    base = dict(
        shape="circle",
        radius_m=1.2,
        period_s=T,
        direction=1,
        start_deg=0.0,
        height_deg=0.0,
        pitch_deg=0.0,
        roll_deg=0.0,
        yaw_deg=0.0,
        aspect=0.6,
        swing_deg=120.0,
        lift_deg=30.0,
    )
    base.update(kw)
    return OrbitParams(**base)


def ang_diff(a, b):
    """环形角度差（度），处理 0/360 边界。"""
    return np.abs((np.asarray(a) - np.asarray(b) + 180.0) % 360.0 - 180.0)


def at(p, t, t_ref=0.0, offset=0.0):
    az, el, d = orbit_position(p, np.array([t]), t_ref, offset)
    return float(az[0]), float(el[0]), float(d[0])


def test_circle_quarter_turn_clockwise_is_right():
    az, el, d = at(params(), T / 4)
    assert ang_diff(az, 90) < 1e-9 and abs(el) < 1e-9 and d == pytest.approx(1.2)


def test_counter_clockwise_quarter_turn_is_left():
    az, _, _ = at(params(direction=-1), T / 4)
    assert ang_diff(az, 270) < 1e-9


def test_t_ref_is_front_and_offset_shifts_phase():
    assert ang_diff(at(params(), 5.0, t_ref=5.0)[0], 0) < 1e-9
    assert ang_diff(at(params(), 5.0, t_ref=5.0, offset=-20)[0], 340) < 1e-9


def test_ellipse_is_closer_in_front_than_at_sides():
    p = params(shape="ellipse", aspect=0.5)
    az0, _, d0 = at(p, 0.0)
    az1, _, d1 = at(p, T / 4)
    assert ang_diff(az0, 0) < 1e-9 and d0 == pytest.approx(0.6)
    assert ang_diff(az1, 90) < 1e-9 and d1 == pytest.approx(1.2)


def test_pendulum_swings_between_plus_minus_swing():
    p = params(shape="pendulum", swing_deg=70)
    assert ang_diff(at(p, T / 4)[0], 70) < 1e-9
    assert ang_diff(at(p, 3 * T / 4)[0], 290) < 1e-9
    assert ang_diff(at(p, T / 2)[0], 0) < 1e-9


def test_figure8_lifts_at_eighth_turn():
    p = params(shape="figure8", swing_deg=90, lift_deg=40, height_deg=0)
    _, el, _ = at(p, T / 8)
    assert el == pytest.approx(40.0, abs=1e-9)


def test_spiral_reaches_full_lift_after_one_turn():
    p = params(shape="spiral", lift_deg=30)
    az, el, _ = at(p, T)
    assert ang_diff(az, 0) < 1e-9 and el == pytest.approx(30.0, abs=1e-9)


def test_fixed_ignores_time():
    p = params(shape="fixed", start_deg=45)
    assert ang_diff(at(p, 0.0)[0], 45) < 1e-9
    assert ang_diff(at(p, 3.3)[0], 45) < 1e-9
    assert ang_diff(at(p, 3.3, offset=10)[0], 55) < 1e-9


def test_pitch_90_lifts_front_point_to_top():
    _, el, _ = at(params(pitch_deg=90), 0.0)
    assert el == pytest.approx(90.0, abs=1e-6)


def test_roll_90_lifts_right_point_to_top():
    _, el, _ = at(params(roll_deg=90), T / 4)
    assert el == pytest.approx(90.0, abs=1e-6)


def test_roll_90_keeps_front_point_in_front():
    az, el, _ = at(params(roll_deg=90), 0.0)
    assert ang_diff(az, 0) < 1e-9 and abs(el) < 1e-9


def test_yaw_rotates_clockwise():
    assert ang_diff(at(params(yaw_deg=30), 0.0)[0], 30) < 1e-9
    assert ang_diff(at(params(shape="pendulum", yaw_deg=180, swing_deg=50), T / 4)[0], 230) < 1e-9


def test_vectorized_over_time():
    t = np.linspace(0, T, 9)
    az, el, d = orbit_position(params(), t, 0.0)
    assert az.shape == el.shape == d.shape == (9,)
    assert ang_diff(az, np.linspace(0, 360, 9)).max() < 1e-9


@pytest.mark.parametrize(
    ("bpm", "expected"), [(180, 90), (90, 90), (67, 134), (45, 90), (139.9, 139.9), (140, 70)]
)
def test_normalize_bpm(bpm, expected):
    assert normalize_bpm(bpm) == pytest.approx(expected)


def test_period_from_bars_and_seconds():
    assert period_seconds("bars", 2, 10.0, 90.0) == pytest.approx(16 / 3)
    assert period_seconds("seconds", 2, 10.0, 90.0) == pytest.approx(10.0)
    with pytest.raises(ValueError):
        period_seconds("minutes", 2, 10.0, 90.0)
