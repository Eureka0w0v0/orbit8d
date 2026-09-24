"""双耳混响的冲激响应（BRIR）合成（SPEC §5.3、§13.7）。

扩散尾巴：16 个水平方向各一条去相关噪声，按频段用房间 RT60 衰减，分别过该方向的 HRIR 后相加，
做 150 Hz 高通（混响不带低频轰鸣），按每耳能量归一。
早期反射：8 个不同方向（含上下）的离散反射，5–25 ms 到达、越晚越弱，同样高通；叠加在尾巴上，
总能量比尾巴高 3 dB。它让声音“出了脑袋”、更有空间感（两耳相关度下降），尾巴的量不变。
导出与浏览器试听用同一份 BRIR。
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
BRIR_VERSION = 2  # 算法变了就加一：浏览器端的缓存文件名跟着变
EARLY_REFLECTIONS = (  # (方位角°, 仰角°, 到达时间 ms)：左右前后上下都有，时间错开
    (30.0, 0.0, 5.3),
    (-40.0, 5.0, 7.1),
    (100.0, 20.0, 9.4),
    (-95.0, -10.0, 11.2),
    (170.0, 10.0, 13.9),
    (-150.0, 35.0, 16.8),
    (60.0, 65.0, 20.3),
    (-15.0, -45.0, 24.1),
)
ER_SLOPE_DB_PER_MS = 0.3  # 每晚 1 ms 弱 0.3 dB
ER_ENERGY_DB = 3.0  # 早期反射总能量比尾巴高 3 dB（每耳平均）


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


def _unit_energy(x: np.ndarray, sr: int) -> np.ndarray:
    """150 Hz 高通后按每耳平均能量归一。"""
    x = sosfilt(butter(2, SEND_HIGHPASS_HZ, "highpass", fs=sr, output="sos"), x, axis=1)
    return x / np.sqrt((x**2).sum(axis=1).mean())


def early_reflections(grid: HrtfGrid, length: int, sr: int) -> np.ndarray:
    """(2, length)，每耳平均能量为 1。"""
    az = np.mod([d[0] for d in EARLY_REFLECTIONS], 360.0)
    el = np.array([d[1] for d in EARLY_REFLECTIONS])
    hrirs = interpolate(grid, az, el).astype(np.float64)
    out = np.zeros((2, length))
    first = EARLY_REFLECTIONS[0][2]
    for k, (_, _, ms) in enumerate(EARLY_REFLECTIONS):
        start = int(round(ms * sr / 1000))
        out[:, start : start + grid.taps] += 10 ** (-ER_SLOPE_DB_PER_MS * (ms - first) / 20) * hrirs[k]
    return _unit_energy(out, sr)


def synth_brir(grid: HrtfGrid, room: str, sr: int) -> np.ndarray:
    """返回 (2, 长度) float32：扩散尾巴（每耳能量 1）+ 早期反射（能量为尾巴的 10^(3/10) 倍）。"""
    if room not in ROOMS:
        raise ValueError(f"未知房间类型: {room}")
    preset = ROOMS[room]
    n = int(min(max(preset.rt60) * TAIL_FACTOR, MAX_TAIL_S) * sr)
    pre = int(round(preset.predelay_s * sr))
    dirs = np.arange(DIRECTIONS) * (360.0 / DIRECTIONS)
    hrirs = interpolate(grid, dirs, np.zeros(DIRECTIONS)).astype(np.float64)
    tail = np.zeros((2, pre + n + grid.taps - 1))
    rng = np.random.default_rng(SEED)
    for k in range(DIRECTIONS):
        noise = _tail(rng, preset, n, sr)
        for ear in (0, 1):
            tail[ear, pre:] += np.convolve(noise, hrirs[k, ear])
    tail = _unit_energy(tail, sr)
    early = early_reflections(grid, tail.shape[1], sr) * 10 ** (ER_ENERGY_DB / 20)
    return (tail + early).astype(np.float32)
