"""母带处理（SPEC §5.4、§13.6）：HRTF 平均音色补偿 EQ（可叠加按场景的频带校正）、
自动总增益、前视真峰值限幅。"""

import numpy as np
import pyloudnorm as pyln
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import firwin2, oaconvolve, resample_poly

from orbit8d.engine.hrtf import HrtfGrid

EQ_LIMIT_DB = 6.0
EQ_TAPS = 1025
EQ_NFFT = 8192
EQ_REF_BAND_HZ = (200.0, 800.0)
THIRD_OCTAVE_HALF = 2 ** (1 / 6)
TARGET_LUFS = -9.0
CEILING_DBTP = -1.0
LIMIT_ALLOW_DB = 1.0  # 只允许约 1% 的 30 ms 片段被压超过 1 dB（保住动态，代价是整体略小声）
LIMIT_PERCENTILE = 99.0
LIMITER_WINDOW_S = 0.03
OVERSAMPLE = 4
EPS = 1e-12


def diffuse_gain_db(grid: HrtfGrid) -> tuple[np.ndarray, np.ndarray]:
    """(频点, 增益 dB)：水平一圈 HRTF 平均功率谱的倒数，1/3 倍频程平滑、限幅 ±6 dB。"""
    row = int(np.argmin(np.abs(grid.el_nodes)))
    spec = np.fft.rfft(grid.data[row].astype(np.float64), EQ_NFFT, axis=-1)
    power = (np.abs(spec) ** 2).mean(axis=(0, 1))
    freqs = np.fft.rfftfreq(EQ_NFFT, 1 / grid.sample_rate)
    cum = np.concatenate([[0.0], np.cumsum(power)])
    lo = np.searchsorted(freqs, freqs / THIRD_OCTAVE_HALF)
    hi = np.maximum(np.searchsorted(freqs, freqs * THIRD_OCTAVE_HALF, side="right"), lo + 1)
    smooth = (cum[hi] - cum[lo]) / (hi - lo)
    ref = smooth[(freqs >= EQ_REF_BAND_HZ[0]) & (freqs <= EQ_REF_BAND_HZ[1])].mean()
    return freqs, np.clip(-10 * np.log10(smooth / ref), -EQ_LIMIT_DB, EQ_LIMIT_DB)


def design_eq(freqs: np.ndarray, gain_db: np.ndarray, sr: int) -> np.ndarray:
    """任意增益曲线 → 线性相位 FIR（EQ_TAPS 抽头）。"""
    return firwin2(EQ_TAPS, freqs / (sr / 2), 10 ** (gain_db / 20))


def diffuse_eq(grid: HrtfGrid) -> np.ndarray:
    return design_eq(*diffuse_gain_db(grid), grid.sample_rate)


def with_band_gains(
    freqs: np.ndarray, base_db: np.ndarray, bands_hz: np.ndarray, band_db: np.ndarray
) -> np.ndarray:
    """在基础曲线上叠加按频带给的增益（对数频率线性插值，频带范围外沿用两端的值）。"""
    return base_db + np.interp(np.log2(np.maximum(freqs, 1.0)), np.log2(bands_hz), band_db)


def apply_eq(x: np.ndarray, fir: np.ndarray) -> np.ndarray:
    """线性相位 FIR，补偿 (taps-1)/2 的延迟，输出与输入等长对齐。"""
    delay = (len(fir) - 1) // 2
    return oaconvolve(x, fir[:, None], axes=0)[delay : delay + len(x)]


def true_peak(x: np.ndarray) -> np.ndarray:
    """4 倍过采样估计每个采样点附近的真峰值（两声道取大）。"""
    up = resample_poly(x, OVERSAMPLE, 1, axis=0)[: OVERSAMPLE * len(x)]
    return np.abs(up).max(axis=1).reshape(len(x), OVERSAMPLE).max(axis=1)


def master_gain(x: np.ndarray, sr: int) -> float:
    """取较小者：到目标响度的增益 / 让 99% 的 30 ms 片段被压不超过 1 dB 的增益。"""
    w = int(LIMITER_WINDOW_S * sr)
    peak = true_peak(x)
    blocks = peak[: len(peak) // w * w].reshape(-1, w).max(axis=1)
    by_peak = 10 ** ((CEILING_DBTP + LIMIT_ALLOW_DB) / 20) / max(np.percentile(blocks, LIMIT_PERCENTILE), EPS)
    loudness = pyln.Meter(sr).integrated_loudness(x)
    if not np.isfinite(loudness):
        return float(by_peak)
    return float(min(by_peak, 10 ** ((TARGET_LUFS - loudness) / 20)))


def limiter(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """前视限幅：最小值滤波 + 平滑，保证增益曲线处处不超过所需衰减。"""
    ceiling = 10 ** (CEILING_DBTP / 20)
    gain = np.minimum(1.0, ceiling / np.maximum(true_peak(x), EPS))
    if np.all(gain == 1.0):
        return x, gain
    w = int(LIMITER_WINDOW_S * sr)
    gain = uniform_filter1d(minimum_filter1d(gain, 2 * w + 1, mode="nearest"), w, mode="nearest")
    return x * gain[:, None], gain
