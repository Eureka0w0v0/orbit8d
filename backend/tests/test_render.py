"""分块双线性 HRTF 渲染（SPEC §5.2）。"""

import numpy as np
import pytest

from orbit8d.assets import HRTF_FILE
from orbit8d.config import load_settings
from orbit8d.engine.hrtf import HrtfGrid, from_sofa, interpolate
from orbit8d.engine.render import (
    BLOCK,
    NFFT,
    BlockPath,
    block_times,
    distance_gain,
    grid_spectra,
    rear_factor,
    rear_shelf,
    render_path,
)

SR = 44100
SOFA = load_settings().assets_dir / HRTF_FILE
needs_sofa = pytest.mark.skipif(not SOFA.exists(), reason="需要先运行 make assets")


def synthetic_grid(taps: int = 128, seed: int = 1) -> HrtfGrid:
    rng = np.random.default_rng(seed)
    data = rng.standard_normal((5, 8, 2, taps)).astype(np.float32) * np.exp(-np.arange(taps) / 20)
    return HrtfGrid(
        sample_rate=SR,
        az_step_deg=45.0,
        el_nodes=np.array([-60.0, -30.0, 0.0, 30.0, 60.0]),
        data=data.astype(np.float32),
    )


def const_path(nb: int, az: float, el: float, gain: float = 1.0, rear: float = 0.0) -> BlockPath:
    return BlockPath(az=np.full(nb, az), el=np.full(nb, el), gain=np.full(nb, gain), rear=np.full(nb, rear))


def err_db(a: np.ndarray, ref: np.ndarray) -> float:
    """误差能量相对参考能量（dB）；完全相同时返回 -inf 而不是报除零警告。"""
    diff = np.sum((a - ref) ** 2)
    return float("-inf") if diff == 0 else float(10 * np.log10(diff / np.sum(ref**2)))


def test_block_times_are_block_centers():
    t = block_times(300, SR, 128)
    assert len(t) == 3
    assert t[0] == pytest.approx(64 / SR) and t[2] == pytest.approx((256 + 64) / SR)
    assert len(block_times(300, SR)) == -(-300 // BLOCK)


def test_fixed_direction_equals_direct_convolution():
    g = synthetic_grid()
    x = np.random.default_rng(2).standard_normal(5000)
    nb = len(block_times(len(x), SR))
    y = render_path(x, const_path(nb, 67.5, 15.0), grid_spectra(g, NFFT), g)
    h = interpolate(g, np.array([67.5]), np.array([15.0]))[0].astype(np.float64)
    ref = np.stack([np.convolve(x, h[e])[: len(x)] for e in (0, 1)], axis=1)
    assert err_db(y, ref) < -100


def test_block_gain_scales_output_linearly():
    g = synthetic_grid()
    x = np.random.default_rng(3).standard_normal(3000)
    nb = len(block_times(len(x), SR))
    spectra = grid_spectra(g, NFFT)
    a = render_path(x, const_path(nb, 10.0, 0.0, gain=1.0), spectra, g)
    b = render_path(x, const_path(nb, 10.0, 0.0, gain=0.25), spectra, g)
    assert err_db(b, 0.25 * a) < -120


def test_rear_factor_and_distance_gain():
    az = np.array([0.0, 90.0, 180.0, 180.0, 135.0])
    el = np.array([0.0, 0.0, 0.0, 90.0, 0.0])
    assert rear_factor(az, el) == pytest.approx([0.0, 0.0, 1.0, 0.0, np.sqrt(0.5)], abs=1e-12)
    assert distance_gain(np.array([0.5, 1.0, 2.0, 8.0, 0.1])) == pytest.approx([2.0, 1.0, 0.5, 0.25, 2.0])


def tone_gain_db(freq: float, rear: float, cut_db: float) -> float:
    n = 44100
    x = np.sin(2 * np.pi * freq * np.arange(n) / SR)
    nb = len(block_times(n, SR))
    y = rear_shelf(x, np.full(nb, rear), cut_db, SR)
    seg = slice(n // 2, n)
    return float(20 * np.log10(np.std(y[seg]) / np.std(x[seg])))


def test_rear_shelf_only_darkens_highs_when_behind():
    assert abs(tone_gain_db(10000, 0.0, 6.0)) < 1e-9
    assert -6.5 < tone_gain_db(10000, 1.0, 6.0) < -4.0
    assert tone_gain_db(200, 1.0, 6.0) > -0.3


@needs_sofa
@pytest.mark.slow
def test_moving_source_is_insensitive_to_block_size():
    """方向逐块切换不产生杂音：默认块长与 16 采样块的差异应远低于可闻阈。"""
    g = from_sofa(SOFA, SR)
    x = np.random.default_rng(4).standard_normal(SR * 3) * 0.1
    outs = []
    for block in (BLOCK, 16):
        t = block_times(len(x), SR, block)
        path = BlockPath(
            az=360.0 * t / 6.0,
            el=20.0 * np.sin(2 * np.pi * t / 6.0),
            gain=np.ones(len(t)),
            rear=np.zeros(len(t)),
        )
        outs.append(render_path(x, path, grid_spectra(g, NFFT), g, block=block, nfft=NFFT))
    assert BLOCK == 32 and NFFT % 16 == 0
    assert err_db(outs[0], outs[1]) < -45
