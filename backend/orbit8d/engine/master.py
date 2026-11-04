"""母带处理（SPEC §5.4、§13.6）：HRTF 平均音色补偿 EQ（可叠加按场景的频带校正）、
自动总增益、前视真峰值限幅。都可以分段做（SPEC §5.7）：响度按 BS.1770 逐段累计，
真峰值、EQ 与限幅给每段前后补上下文，结果与整段处理一致。"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np
from pyloudnorm.iirfilter import IIRfilter
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import firwin2, lfilter, oaconvolve, resample_poly

from orbit8d.engine.chunks import CHUNK, BlockMax, SegmentSums, chunk_ranges
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
TRUE_PEAK_MARGIN = 16  # 4 倍过采样的插值滤波器只用到前后各 10 个采样
TRUE_PEAK_BLOCK = 65536  # 真峰值分块算，临时内存与整曲长短无关
LIMIT_MARGIN = 2048  # 限幅增益用到前后各约 2000 个采样：真峰值 10 + 最小值滤波 1323 + 平滑 661
EPS = 1e-12

# ITU-R BS.1770 整体响度（与 pyloudnorm 0.2 的 Meter.integrated_loudness 逐项一致）
K_WEIGHTING = ((4.0, 1 / np.sqrt(2), 1500.0, "high_shelf"), (0.0, 0.5, 38.0, "high_pass"))
GATE_BLOCK_S = 0.4
GATE_STEP = 1.0 - 0.75  # 门限块重叠 75%
ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = -10.0
CHANNEL_WEIGHTS = (1.0, 1.0, 1.0, 1.41, 1.41)
LUFS_OFFSET = -0.691

Window = Callable[[int, int], np.ndarray]  # window(a, b)：信号 [a, b) 段，float64 (帧, 声道)


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


def eq_window(x: np.ndarray, fir: np.ndarray, a: int, b: int) -> np.ndarray:
    """apply_eq(x, fir)[a:b]，只用到 x 在 [a - 延迟, b + 延迟) 内的部分（float64）。"""
    delay = (len(fir) - 1) // 2
    c0, c1 = max(0, a - delay), min(len(x), b + delay)
    full = oaconvolve(x[c0:c1].astype(np.float64), fir[:, None], axes=0)
    return full[a + delay - c0 : b + delay - c0]


def true_peak(x: np.ndarray) -> np.ndarray:
    """4 倍过采样估计每个采样点附近的真峰值（两声道取大）。分块算、每块前后补上下文，结果与整段一次算相同。"""
    n = len(x)
    out = np.empty(n)
    for s, e in chunk_ranges(n, TRUE_PEAK_BLOCK):
        a, b = max(0, s - TRUE_PEAK_MARGIN), min(n, e + TRUE_PEAK_MARGIN)
        up = resample_poly(x[a:b], OVERSAMPLE, 1, axis=0)[: OVERSAMPLE * (b - a)]
        out[s:e] = np.abs(up).max(axis=1).reshape(b - a, OVERSAMPLE).max(axis=1)[s - a : e - a]
    return out


class LoudnessMeter:
    """BS.1770 整体响度的分段版：按顺序喂 (帧, 声道)；K 计权滤波器状态带进下一段，门限块按整曲位置累计。"""

    def __init__(self, n_total: int, channels: int, sr: int):
        self._stages = []
        for gain_db, q, fc, kind in K_WEIGHTING:
            f = IIRfilter(gain_db, q, fc, sr, kind)
            b, a = f.b, f.a
            self._stages.append((b, a, f.passband_gain, np.zeros((channels, max(len(a), len(b)) - 1))))
        self._norm = 1.0 / (GATE_BLOCK_S * sr)
        self._short = n_total < GATE_BLOCK_S * sr  # 比一个门限块还短：测不出来
        span = (n_total / sr - GATE_BLOCK_S) / (GATE_BLOCK_S * GATE_STEP)
        j = np.arange(0 if self._short else int(np.round(span)) + 1)
        self._lo = (GATE_BLOCK_S * (j * GATE_STEP) * sr).astype(np.int64)
        self._hi = np.minimum((GATE_BLOCK_S * (j * GATE_STEP + 1) * sr).astype(np.int64), n_total)
        bounds = np.unique(np.concatenate([[0, n_total], self._lo, self._hi]))
        self._sums = SegmentSums(bounds, channels)

    def push(self, x: np.ndarray) -> None:
        y = np.array(x, dtype=np.float64).reshape(len(x), -1)
        for b, a, gain, zi in self._stages:
            for ch in range(y.shape[1]):
                out, zi[ch] = lfilter(b, a, y[:, ch], zi=zi[ch])
                y[:, ch] = gain * out
        self._sums.push(y**2)

    def result(self) -> float:
        """LUFS；整曲比门限块短、或没有一块高于绝对门限时返回 -inf。"""
        if self._short:
            return float("-inf")
        sums = np.array([self._sums.between(a, b) for a, b in zip(self._lo, self._hi, strict=True)])
        return gated_loudness(self._norm * sums.T)


def gated_loudness(z: np.ndarray) -> float:
    """z：(声道, 门限块) 的均方值 → 先绝对门限 -70 LUFS、再相对门限 -10 LU 后的整体响度。"""
    weights = np.array(CHANNEL_WEIGHTS[: len(z)])
    with np.errstate(divide="ignore"):
        block = LUFS_OFFSET + 10.0 * np.log10((weights[:, None] * z).sum(axis=0))
    absolute = block >= ABSOLUTE_GATE_LUFS
    if not absolute.any():
        return float("-inf")
    relative = LUFS_OFFSET + 10.0 * np.log10((weights * z[:, absolute].mean(axis=1)).sum()) + RELATIVE_GATE_LU
    chosen = (block > relative) & (block > ABSOLUTE_GATE_LUFS)
    if not chosen.any():
        return float("-inf")
    return float(LUFS_OFFSET + 10.0 * np.log10((weights * z[:, chosen].mean(axis=1)).sum()))


def integrated_loudness(x: np.ndarray, sr: int) -> float:
    """ITU-R BS.1770 整体响度（LUFS）；全静音、太短等测不出来时返回 -inf。"""
    x = np.asarray(x)
    meter = LoudnessMeter(len(x), 1 if x.ndim == 1 else x.shape[1], sr)
    for s, e in chunk_ranges(len(x)):
        meter.push(x[s:e])
    return meter.result()


@dataclass(frozen=True)
class MasterStats:
    block_peaks: np.ndarray  # 每个完整 30 ms 片段的真峰值
    loudness: float  # 整体响度（LUFS，测不出来时为 -inf）


def measure(window: Window, n: int, sr: int, channels: int = 2, size: int = CHUNK) -> MasterStats:
    """逐段测待测信号的 30 ms 片段真峰值与整体响度（window 见类型 Window）。"""
    blocks = BlockMax(int(LIMITER_WINDOW_S * sr))
    meter = LoudnessMeter(n, channels, sr)
    for s, e in chunk_ranges(n, size):
        a, b = max(0, s - TRUE_PEAK_MARGIN), min(n, e + TRUE_PEAK_MARGIN)
        x = window(a, b)
        blocks.push(true_peak(x)[s - a : e - a])
        meter.push(x[s - a : e - a])
    return MasterStats(blocks.result(), meter.result())


def gain_for(stats: MasterStats, target_lufs: float = TARGET_LUFS) -> float:
    """取较小者：到目标响度的增益 / 让 99% 的 30 ms 片段被压不超过 1 dB 的增益。"""
    p99 = np.percentile(stats.block_peaks, LIMIT_PERCENTILE)
    by_peak = 10 ** ((CEILING_DBTP + LIMIT_ALLOW_DB) / 20) / max(p99, EPS)
    if not np.isfinite(stats.loudness):
        return float(by_peak)
    return float(min(by_peak, 10 ** ((target_lufs - stats.loudness) / 20)))


def master_gain(x: np.ndarray, sr: int, target_lufs: float = TARGET_LUFS) -> float:
    return gain_for(measure(lambda a, b: x[a:b].astype(np.float64), len(x), sr), target_lufs)


def limiter_gain(x: np.ndarray, sr: int) -> np.ndarray:
    """逐采样增益：真峰值超过上限处所需的衰减 → 前后 30 ms 取最小 → 30 ms 平滑，保证处处不超过所需衰减。"""
    ceiling = 10 ** (CEILING_DBTP / 20)
    gain = np.minimum(1.0, ceiling / np.maximum(true_peak(x), EPS))
    if np.all(gain == 1.0):
        return gain
    w = int(LIMITER_WINDOW_S * sr)
    return uniform_filter1d(minimum_filter1d(gain, 2 * w + 1, mode="nearest"), w, mode="nearest")


def limited(
    window: Window, n: int, sr: int, gain: float = 1.0, size: int = CHUNK
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """逐段给出 (gain × 信号经前视限幅后的结果, 限幅增益)，与对整段做限幅一致。"""
    for s, e in chunk_ranges(n, size):
        a, b = max(0, s - LIMIT_MARGIN), min(n, e + LIMIT_MARGIN)
        x = window(a, b) * gain
        g = limiter_gain(x, sr)
        yield (x * g[:, None])[s - a : e - a], g[s - a : e - a]


def limiter(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """前视限幅：最小值滤波 + 平滑，保证增益曲线处处不超过所需衰减。"""
    parts = list(limited(lambda a, b: x[a:b].astype(np.float64), len(x), sr))
    return np.concatenate([p[0] for p in parts]), np.concatenate([p[1] for p in parts])
