"""混响冲激响应（SPEC §5.3）与母带处理（SPEC §5.4）。"""

import numpy as np
import pytest
from scipy.signal import butter, sosfiltfilt

from orbit8d.engine.hrtf import HrtfGrid, interpolate
from orbit8d.engine.master import (
    CEILING_DBTP,
    apply_eq,
    diffuse_eq,
    limiter,
    master_gain,
    true_peak,
)
from orbit8d.engine.reverb import (
    DIRECTIONS,
    EARLY_REFLECTIONS,
    ER_ENERGY_DB,
    ROOMS,
    early_reflections,
    synth_brir,
)

SR = 44100


def flat_grid(taps: int = 64) -> HrtfGrid:
    """每个方向都是单位脉冲（无染色）的假 HRTF。"""
    data = np.zeros((3, 8, 2, taps), dtype=np.float32)
    data[..., 0] = 1.0
    return HrtfGrid(sample_rate=SR, az_step_deg=45.0, el_nodes=np.array([-30.0, 0.0, 30.0]), data=data)


def lateral_grid(taps: int = 64) -> HrtfGrid:
    """左右耳增益随方向变化的假 HRTF：左侧来的声音主要进左耳，右侧来的进右耳。"""
    data = np.zeros((3, 8, 2, taps), dtype=np.float32)
    side = np.sin(np.radians(np.arange(8) * 45.0))
    data[:, :, 0, 0] = 0.05 + (1 - side) / 2
    data[:, :, 1, 0] = 0.05 + (1 + side) / 2
    return HrtfGrid(sample_rate=SR, az_step_deg=45.0, el_nodes=np.array([-30.0, 0.0, 30.0]), data=data)


@pytest.fixture(scope="module")
def hall():
    return synth_brir(flat_grid(), "hall", SR)


def schroeder_rt60(ir: np.ndarray, lo: float, hi: float) -> float:
    band = sosfiltfilt(butter(4, [lo, hi], "bandpass", fs=SR, output="sos"), ir, axis=-1)
    energy = (band**2).sum(axis=0)
    edc = np.cumsum(energy[::-1])[::-1]
    edc_db = 10 * np.log10(edc / edc[0] + 1e-30)
    i5, i35 = np.argmax(edc_db < -5), np.argmax(edc_db < -35)
    return float(60.0 * (i35 - i5) / SR / 30.0)


def test_brir_is_normalized_and_deterministic(hall):
    """尾巴每耳能量 1，早期反射再叠加 10^(3/10) 倍：总能量约 1 + 2（两者不相关）。"""
    assert hall.shape[0] == 2 and hall.dtype == np.float32 and np.isfinite(hall).all()
    total = (hall.astype(np.float64) ** 2).sum(axis=1).mean()
    assert total == pytest.approx(1.0 + 10 ** (ER_ENERGY_DB / 10), rel=0.05)
    assert np.array_equal(hall, synth_brir(flat_grid(), "hall", SR))


def test_early_reflections_arrive_between_5_and_25_ms_from_many_directions():
    grid = lateral_grid()
    er = early_reflections(grid, SR // 10, SR)
    assert (er**2).sum(axis=1).mean() == pytest.approx(1.0, rel=1e-6)
    first = min(ms for *_, ms in EARLY_REFLECTIONS)
    assert np.abs(er[:, : int(first * SR / 1000) - 1]).max() == 0.0  # 5 ms 之前完全安静
    energy = (er**2).cumsum(axis=1).sum(axis=0)
    assert energy[int(0.026 * SR)] / energy[-1] > 0.95  # 能量基本都在 25 ms 以内（高通尾巴除外）
    left, right = (er[0] ** 2).sum(), (er[1] ** 2).sum()
    assert 0.5 < left / right < 2.0  # 左右两侧都有反射
    assert np.corrcoef(er[0], er[1])[0, 1] < 0.9  # 两耳不是同一个信号：带来空间感


def test_brir_tails_are_independent_per_direction():
    """各方向噪声尾巴相互独立时，两耳相关系数 = Σgl·gr / √(Σgl²·Σgr²)；若共用同一条噪声则为 1。"""
    grid = lateral_grid()
    brir = synth_brir(grid, "hall", SR)
    dirs = np.arange(DIRECTIONS) * (360.0 / DIRECTIONS)
    g = interpolate(grid, dirs, np.zeros(DIRECTIONS))[:, :, 0].astype(np.float64)
    expected = (g[:, 0] * g[:, 1]).sum() / np.sqrt((g[:, 0] ** 2).sum() * (g[:, 1] ** 2).sum())
    assert np.corrcoef(brir[0], brir[1])[0, 1] == pytest.approx(expected, abs=0.05)
    assert expected < 0.6


def test_brir_midband_decay_matches_preset(hall):
    rt = schroeder_rt60(hall, 1000, 4000)
    assert rt == pytest.approx(ROOMS["hall"].rt60[2], rel=0.3)


def test_rooms_are_ordered_by_decay():
    rts = [
        schroeder_rt60(synth_brir(flat_grid(), name, SR), 250, 1000) for name in ("room", "hall", "church")
    ]
    assert rts[0] < rts[1] < rts[2]


def test_brir_has_no_low_rumble(hall):
    spec = np.abs(np.fft.rfft(hall, axis=1)) ** 2
    f = np.fft.rfftfreq(hall.shape[1], 1 / SR)
    low = spec[:, (f > 30) & (f < 60)].mean()
    mid = spec[:, (f > 400) & (f < 800)].mean()
    assert 10 * np.log10(low / mid) < -12


def test_unknown_room_is_rejected():
    with pytest.raises(ValueError):
        synth_brir(flat_grid(), "stadium", SR)


def test_eq_of_uncolored_hrtf_is_transparent():
    fir = diffuse_eq(flat_grid())
    x = np.random.default_rng(0).standard_normal((SR, 2))
    y = apply_eq(x, fir)
    assert y.shape == x.shape
    err = np.sum((y[2000:-2000] - x[2000:-2000]) ** 2) / np.sum(x[2000:-2000] ** 2)
    assert 10 * np.log10(err) < -30


def test_limiter_enforces_true_peak_ceiling():
    x = np.random.default_rng(1).standard_normal((SR * 2, 2)) * 0.8
    y, gain = limiter(x, SR)
    assert gain.max() <= 1.0 and 20 * np.log10(true_peak(y).max()) <= CEILING_DBTP + 0.05


def test_limiter_leaves_quiet_signal_untouched():
    x = np.random.default_rng(2).standard_normal((SR, 2)) * 0.05
    y, gain = limiter(x, SR)
    assert np.array_equal(y, x) and np.all(gain == 1.0)


def test_master_gain_is_capped_by_target_loudness():
    t = np.arange(SR * 5) / SR
    tone = np.stack([np.sin(2 * np.pi * 1000 * t)] * 2, axis=1) * 0.01  # 很安静、波峰系数低
    g_quiet = master_gain(tone, SR)
    g_loud = master_gain(tone * 10, SR)
    assert g_quiet == pytest.approx(10 * g_loud, rel=1e-3)  # 由目标响度决定，与电平成反比
