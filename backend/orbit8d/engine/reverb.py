"""双耳扩散混响的冲激响应（BRIR）合成（SPEC §5.3）。

16 个水平方向各一条去相关噪声尾巴，按频段用房间 RT60 衰减，分别过该方向的 HRIR 后相加；
再做 150 Hz 高通（混响不带低频轰鸣），按每耳能量归一。导出与浏览器试听用同一份 BRIR。
"""

from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfilt, sosfiltfilt

from orbit8d.engine.hrtf import HrtfGrid, interpolate

SPLITS_HZ = (250.0, 1000.0, 4000.0, 8000.0)
DIRECTIONS = 16
ONSET_S = 0.008
SEND_HIGHPASS_HZ = 150.0
TAIL_FACTOR = 1.2
MAX_TAIL_S = 5.0
SEED = 2016
DECAY_60DB = np.log(1000.0)  # 能量衰减 60 dB = 幅度衰减 1000 倍


@dataclass(frozen=True)
class RoomPreset:
    rt60: tuple[float, float, float, float, float]  # <250 / 250–1k / 1k–4k / 4k–8k / >8k Hz
    predelay_s: float


ROOMS = {
    "room": RoomPreset(rt60=(0.8, 0.7, 0.6, 0.45, 0.3), predelay_s=0.008),
    "hall": RoomPreset(rt60=(2.2, 2.0, 1.6, 1.1, 0.6), predelay_s=0.018),
    "church": RoomPreset(rt60=(4.0, 3.6, 2.8, 1.8, 0.9), predelay_s=0.030),
}


def _tail(rng: np.random.Generator, preset: RoomPreset, n: int, sr: int) -> np.ndarray:
    t = np.arange(n) / sr
    rest = rng.standard_normal(n)
    tail = np.zeros(n)
    for fc, rt60 in zip((*SPLITS_HZ, None), preset.rt60, strict=True):
        band = rest if fc is None else sosfiltfilt(butter(4, fc, fs=sr, output="sos"), rest)
        rest = rest - band  # 零相位互补分频，各频段相加完全还原
        tail += band * np.exp(-DECAY_60DB * t / rt60)
    return tail * (1.0 - np.exp(-t / ONSET_S))


def synth_brir(grid: HrtfGrid, room: str, sr: int) -> np.ndarray:
    """返回 (2, 长度) float32。"""
    if room not in ROOMS:
        raise ValueError(f"未知房间类型: {room}")
    preset = ROOMS[room]
    n = int(min(max(preset.rt60) * TAIL_FACTOR, MAX_TAIL_S) * sr)
    pre = int(round(preset.predelay_s * sr))
    dirs = np.arange(DIRECTIONS) * (360.0 / DIRECTIONS)
    hrirs = interpolate(grid, dirs, np.zeros(DIRECTIONS)).astype(np.float64)
    brir = np.zeros((2, pre + n + grid.taps - 1))
    rng = np.random.default_rng(SEED)
    for k in range(DIRECTIONS):
        tail = _tail(rng, preset, n, sr)
        for ear in (0, 1):
            brir[ear, pre:] += np.convolve(tail, hrirs[k, ear])
    brir = sosfilt(butter(2, SEND_HIGHPASS_HZ, "highpass", fs=sr, output="sos"), brir, axis=1)
    return (brir / np.sqrt((brir**2).sum(axis=1).mean())).astype(np.float32)
