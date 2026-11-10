"""HRTF 网格：插值、二进制格式、从 SOFA 数据建网格、真实 KU100 数据的物理合理性（SPEC §5.6）。"""

import numpy as np
import pytest
from scipy.signal import resample_poly

from orbit8d.assets import HRTF_FILE
from orbit8d.config import load_settings
from orbit8d.engine.hrtf import TAPS, HrtfGrid, SofaData, from_bytes, from_sofa, interpolate, to_bytes
from orbit8d.media.sofa import read_sofa
from tests.conftest import SOFA_AZ_STEP, SOFA_ELEVATIONS, write_sofa

SR = 44100
SOFA = load_settings().assets_dir / HRTF_FILE
needs_sofa = pytest.mark.skipif(not SOFA.exists(), reason="需要先运行 make assets")


def synthetic_grid(seed: int = 0) -> HrtfGrid:
    rng = np.random.default_rng(seed)
    data = rng.standard_normal((5, 8, 2, 4)).astype(np.float32)
    return HrtfGrid(
        sample_rate=SR, az_step_deg=45.0, el_nodes=np.array([-60.0, -30.0, 0.0, 30.0, 60.0]), data=data
    )


def test_interpolation_is_exact_on_grid_points():
    g = synthetic_grid()
    out = interpolate(g, np.array([0.0, 45.0, 315.0, 90.0]), np.array([0.0, -60.0, 60.0, 30.0]))
    expected = g.data[[2, 0, 4, 3], [0, 1, 7, 2]]
    assert np.abs(out - expected).max() < 1e-6


def test_interpolation_averages_at_midpoints():
    g = synthetic_grid()
    out = interpolate(g, np.array([22.5, 0.0]), np.array([0.0, 15.0]))
    assert np.abs(out[0] - (g.data[2, 0] + g.data[2, 1]) / 2).max() < 1e-6
    assert np.abs(out[1] - (g.data[2, 0] + g.data[3, 0]) / 2).max() < 1e-6


def test_azimuth_wraps_between_last_and_first_column():
    g = synthetic_grid()
    out = interpolate(g, np.array([337.5, 360.0]), np.array([0.0, 0.0]))
    assert np.abs(out[0] - (g.data[2, 7] + g.data[2, 0]) / 2).max() < 1e-6
    assert np.abs(out[1] - g.data[2, 0]).max() < 1e-6


def test_elevation_is_clamped_to_grid():
    g = synthetic_grid()
    out = interpolate(g, np.array([45.0, 45.0]), np.array([80.0, -89.0]))
    assert np.abs(out[0] - g.data[4, 1]).max() < 1e-6
    assert np.abs(out[1] - g.data[0, 1]).max() < 1e-6


def test_binary_roundtrip():
    g = synthetic_grid()
    back = from_bytes(to_bytes(g))
    assert back.sample_rate == g.sample_rate and back.az_step_deg == g.az_step_deg
    assert np.array_equal(back.el_nodes, g.el_nodes) and np.array_equal(back.data, g.data)


def test_from_bytes_rejects_bad_magic():
    with pytest.raises(ValueError):
        from_bytes(b"NOPE" + b"\x00" * 16)


def test_a_sofa_file_becomes_a_clockwise_grid_normalised_on_the_horizontal_ring(tmp_path):
    positions, ir = write_sofa(tmp_path / "small.sofa")
    grid = from_sofa(read_sofa(tmp_path / "small.sofa"), SR)  # 文件就是 44.1 kHz：不重采样
    assert grid.data.shape == (3, 8, 2, TAPS) and grid.data.dtype == np.float32
    assert grid.sample_rate == SR and grid.az_step_deg == SOFA_AZ_STEP
    assert grid.el_nodes.tolist() == list(SOFA_ELEVATIONS)
    assert np.all(grid.data[..., ir.shape[-1] :] == 0)  # 不够 128 点的补零
    assert (grid.data[1].astype(np.float64) ** 2).sum(-1).mean() == pytest.approx(1.0, abs=1e-6)
    level = np.sqrt((ir[positions[:, 1] == 0.0] ** 2).sum(-1).mean())  # 水平一圈平均每耳能量归一成 1
    for (az_ccw, el, _), measured in zip(positions, ir, strict=True):
        i = SOFA_ELEVATIONS.index(el)
        j = int(round((360.0 - az_ccw) % 360.0 / SOFA_AZ_STEP)) % 8  # 逆时针方位 → 顺时针索引
        assert np.abs(grid.data[i, j, :, : ir.shape[-1]] * level - measured).max() < 1e-5


def test_a_sofa_file_that_lists_the_right_ear_first_gives_the_same_grid(tmp_path):
    write_sofa(tmp_path / "left.sofa")
    write_sofa(tmp_path / "right.sofa", left_ear_first=False)
    left, right = (from_sofa(read_sofa(tmp_path / name), SR) for name in ("left.sofa", "right.sofa"))
    assert np.array_equal(left.data, right.data)


def test_a_sofa_file_that_is_not_a_full_grid_is_rejected_by_name(tmp_path):
    write_sofa(tmp_path / "holes.sofa", skip=1)
    with pytest.raises(ValueError, match="holes.sofa"):
        from_sofa(read_sofa(tmp_path / "holes.sofa"), SR)


def test_measurements_at_another_rate_are_resampled_and_cut_to_the_grid_length():
    """不经文件，直接给测量数据：48 kHz、300 点、1 ms 处（第 48 个采样）一个脉冲
    → 44.1 kHz、128 点，脉冲仍在 1 ms（第 44 个采样）。"""
    positions = np.array([(k * 90.0, el, 1.0) for el in (-45.0, 45.0) for k in range(4)])
    ir = np.zeros((len(positions), 2, 300))
    ir[:, :, 48] = 1.0
    ears = np.array([[0.0, 0.09, 0.0], [0.0, -0.09, 0.0]])
    grid = from_sofa(SofaData("memory", ir, 48000, positions, ears), SR)
    assert grid.data.shape == (2, 4, 2, TAPS) and grid.sample_rate == SR and grid.az_step_deg == 90.0
    assert set(np.abs(grid.data).argmax(axis=-1).ravel().tolist()) == {44}


def onset_ms(ir: np.ndarray, sr: int = SR) -> float:
    up = np.abs(resample_poly(ir, 8, 1))
    return float(np.argmax(up > 0.2 * up.max()) / (8 * sr) * 1e3)


@pytest.fixture(scope="module")
def ku100() -> HrtfGrid:
    return from_sofa(read_sofa(SOFA), SR)


@needs_sofa
@pytest.mark.slow
def test_ku100_shape_and_horizontal_normalization(ku100):
    assert ku100.data.shape == (89, 180, 2, 128) and ku100.data.dtype == np.float32
    row = int(np.argmin(np.abs(ku100.el_nodes)))
    assert (ku100.data[row].astype(np.float64) ** 2).sum(-1).mean() == pytest.approx(1.0, abs=1e-3)


@needs_sofa
@pytest.mark.slow
def test_ku100_right_side_delays_left_ear(ku100):
    ir = interpolate(ku100, np.array([90.0]), np.array([0.0]))[0]
    itd = onset_ms(ir[0]) - onset_ms(ir[1])
    assert 0.62 <= itd <= 0.70
    assert (ir[1] ** 2).sum() > 4 * (ir[0] ** 2).sum()  # 右耳明显更响


@needs_sofa
@pytest.mark.slow
@pytest.mark.parametrize("az", [30.0, 60.0, 90.0, 120.0, 150.0])
def test_ku100_is_left_right_mirror_symmetric(ku100, az):
    right = interpolate(ku100, np.array([az]), np.array([0.0]))[0]
    left = interpolate(ku100, np.array([360.0 - az]), np.array([0.0]))[0]
    near_r, near_l = (right[1] ** 2).sum(), (left[0] ** 2).sum()
    assert abs(10 * np.log10(near_r / near_l)) < 1.5
