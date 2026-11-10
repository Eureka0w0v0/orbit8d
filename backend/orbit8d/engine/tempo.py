"""测速与默认转速（SPEC §4.2）：鼓轨谱通量的周期性 → BPM、拍子相位 → 最接近 7 秒的整小节数。

谱通量可以分段算（OnsetEnvelope）：STFT 帧的位置与整段处理相同，相邻帧的差分跨段接续。
"""

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import stft

from orbit8d.engine.chunks import Framer, chunk_ranges
from orbit8d.engine.orbit import BAR_CHOICES, bar_seconds, normalize_bpm

TEMPO_RANGE_BPM = (60.0, 180.0)
COARSE_STEP_BPM = 0.05
FINE_STEP_BPM = 0.002
BEAT_TOLERANCE_S = 0.04  # 每拍前后 40 ms 内取最强起音
TARGET_TURN_S = 7.0
FALLBACK_BPM = 120.0  # 没有鼓（如清唱）时的默认速度
HOP = 256
NFFT = 2048
DETREND_FRAMES = 64
LOG_COMPRESS = 100.0
SILENCE = 1e-9
STFT_BATCH = 1024  # 谱通量一次最多算多少帧（约 8 MB）


@dataclass(frozen=True)
class TempoInfo:
    bpm: float  # 检测到的速度（可能是 2 倍或 1/2 倍）
    bpm_norm: float  # 归一到 [70, 140)
    t0: float  # 检测速度下第一拍的相位（秒）
    strength: np.ndarray  # 每一拍（检测速度下）的起音强度


class OnsetEnvelope:
    """鼓轨（单声道）的谱通量：按顺序逐段喂入，result() 给出去趋势后的通量与各点时刻。"""

    def __init__(self, sr: int):
        self.sr = sr
        self._framer = Framer(NFFT, HOP)
        self._last: np.ndarray | None = None  # 上一帧的对数幅度谱
        self._parts: list[np.ndarray] = []

    def push(self, x: np.ndarray) -> None:
        span = self._framer.push(x)
        if span is None:
            return
        m = self._framer.count(len(span))
        for k0 in range(0, m, STFT_BATCH):  # 每次最多算 STFT_BATCH 帧，限制临时内存
            k1 = min(m, k0 + STFT_BATCH)
            part = span[k0 * HOP : (k1 - 1) * HOP + NFFT]
            _, _, z = stft(part, self.sr, nperseg=NFFT, noverlap=NFFT - HOP, boundary=None, padded=False)
            mag = np.log1p(LOG_COMPRESS * np.abs(z))
            if self._last is not None:
                self._parts.append(np.maximum(0, mag[:, :1] - self._last).sum(0))
            self._parts.append(np.maximum(0, np.diff(mag, axis=1)).sum(0))
            self._last = mag[:, -1:].copy()

    def result(self) -> tuple[np.ndarray, np.ndarray]:
        flux = np.concatenate(self._parts) if self._parts else np.zeros(0)
        if len(flux):
            flux = np.maximum(0, flux - uniform_filter1d(flux, DETREND_FRAMES))
        t = (np.arange(len(flux)) + 1) * HOP / self.sr + NFFT / (2 * self.sr)
        return flux, t


def estimate_tempo(drums: np.ndarray, sr: int) -> TempoInfo:
    onsets = OnsetEnvelope(sr)
    for s, e in chunk_ranges(len(drums)):
        onsets.push(np.asarray(drums[s:e], dtype=np.float64))
    return tempo_from_onsets(*onsets.result(), sr)


def tempo_from_onsets(flux: np.ndarray, t: np.ndarray, sr: int) -> TempoInfo:
    if len(flux) == 0 or flux.sum() < SILENCE:
        return TempoInfo(FALLBACK_BPM, FALLBACK_BPM, 0.0, np.zeros(0))

    def periodicity(bpms: np.ndarray) -> np.ndarray:
        return np.array([np.sum(flux * np.exp(-2j * np.pi * b / 60.0 * t)) for b in bpms])

    coarse = np.arange(*TEMPO_RANGE_BPM, COARSE_STEP_BPM)
    center = coarse[int(np.argmax(np.abs(periodicity(coarse))))]
    fine = np.arange(center - COARSE_STEP_BPM, center + COARSE_STEP_BPM, FINE_STEP_BPM)
    coef = periodicity(fine)
    best = int(np.argmax(np.abs(coef)))
    bpm = float(fine[best])
    beat = 60.0 / bpm
    t0 = float((-np.angle(coef[best]) / (2 * np.pi) * beat) % beat)
    peak = maximum_filter1d(flux, 2 * int(BEAT_TOLERANCE_S * sr / HOP) + 1)
    strength = np.interp(np.arange(t0, t[-1], beat), t, peak)
    return TempoInfo(bpm, normalize_bpm(bpm), t0, strength)


def turn_plan(info: TempoInfo) -> tuple[int, float]:
    """返回 (默认小节数, 正前方对齐时刻 t_ref)。"""
    bar_s = bar_seconds(info.bpm_norm)
    bars = min(BAR_CHOICES, key=lambda n: abs(n * bar_s - TARGET_TURN_S))
    beat = 60.0 / info.bpm
    per_turn = max(1, round(bars * bar_s / beat))
    if len(info.strength) < per_turn:
        return bars, info.t0
    scores = [info.strength[p::per_turn].mean() for p in range(per_turn)]
    return bars, info.t0 + int(np.argmax(scores)) * beat
