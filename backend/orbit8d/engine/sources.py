"""9 路声源（SPEC §5.1）：声道布局，以及把原曲 + 分轨按 120 Hz 零相位分频成运动部分与超低频。

试听文件、浏览器端的引擎、分析和导出读的是同一份 9 路数据；声道顺序见 SOURCE_CHANNELS。
"""

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt

from orbit8d.engine.chunks import CHUNK, Window, chunk_ranges
from orbit8d.engine.scene import TRACKS

CROSSOVER_HZ = 120.0
CROSSOVER_MARGIN_S = 0.5  # 零相位分频每段前后补 0.5 秒：二阶 120 Hz 滤波器的瞬态衰减到 e^-266，远低于浮点精度
LOW_SPLIT_TRACKS = ("bass", "drums", "other")  # 人声不分频
SEPARATED_TRACKS = ("vocals", "drums", "bass")  # other = 原曲 − 这三轨：分离残差并入 other，一个音都不丢
SOURCE_CHANNELS = (
    "vocals_hi", "bass_hi", "drums_hi_L", "drums_hi_R", "other_hi_L", "other_hi_R",
    "bass_sub", "drums_sub", "other_sub",
)  # fmt: skip
TRACK_CHANNELS = {"vocals": (0,), "drums": (2, 3), "bass": (1,), "other": (4, 5)}  # 立体声轨渲染成一对声源
SUB_CHANNELS = {"bass": 6, "drums": 7, "other": 8}
PREVIEW_FILES = {
    "vocals_hi": (0,), "bass_hi": (1,), "drums_hi": (2, 3), "other_hi": (4, 5),
    "bass_sub": (6,), "drums_sub": (7,), "other_sub": (8,),
}  # fmt: skip
SPECTRA_KEYS = (
    "vocals", "drums_L", "drums_R", "drums_M", "bass", "other_L", "other_R", "other_M",
    "bass_sub", "drums_sub", "other_sub",
)  # fmt: skip
CALIBRATION_KEYS = (*TRACKS, "sub")


@dataclass
class Sources:
    """渲染用的 9 路单声道声源（顺序见 SOURCE_CHANNELS），float32 (9, n)。"""

    data: np.ndarray
    sr: int

    @property
    def n(self) -> int:
        return self.data.shape[1]

    def hi(self, name: str) -> np.ndarray:
        """运动部分：单声道音轨 (n,)，立体声音轨 (n, 2)。"""
        ch = TRACK_CHANNELS[name]
        return self.data[ch[0]] if len(ch) == 1 else self.data[list(ch)].T

    def sub(self, name: str) -> np.ndarray:
        return self.data[SUB_CHANNELS[name]]

    def chunks(self, size: int = CHUNK) -> Iterator[np.ndarray]:
        """按段给出 (9, L) 的 float64 副本。"""
        for s, e in chunk_ranges(self.n, size):
            yield self.data[:, s:e].astype(np.float64)


def split_low(x: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """零相位分频：low + high 完全还原 x。"""
    low = sosfiltfilt(butter(2, CROSSOVER_HZ, fs=sr, output="sos"), x, axis=0)
    return low, x - low


def split_window(
    orig: Window, vocals: Window, drums: Window, bass: Window, sr: int
) -> tuple[np.ndarray, dict[str, float]]:
    """一段原曲 + 三条分轨（带同样的上下文）→ 这一段的 9 路声源 (9, L) 与各轨原始能量（校准目标）。"""
    keep = slice(orig.before, orig.before + orig.end - orig.start)
    stems = {
        "bass": bass.data,
        "drums": drums.data,
        "other": orig.data - vocals.data - drums.data - bass.data,
    }
    out = np.empty((len(SOURCE_CHANNELS), orig.end - orig.start))
    out[0] = vocals.body.mean(axis=1)
    energy = {"vocals": float(np.sum(vocals.body**2))}
    low_total = np.zeros((out.shape[1], 2))
    for name in LOW_SPLIT_TRACKS:
        low, high = (part[keep] for part in split_low(stems[name], sr))
        out[SUB_CHANNELS[name]] = low.mean(axis=1)
        low_total += low
        channels = TRACK_CHANNELS[name]
        out[list(channels)] = high.T if len(channels) == 2 else high.mean(axis=1)
        energy[name] = float(np.sum(high**2))
    energy["sub"] = float(np.sum(low_total**2))
    return out, energy


def spectra_rows(chunk: np.ndarray) -> Iterator[np.ndarray]:
    """(9, L) 声源 → 按 SPECTRA_KEYS 顺序逐路给出单声道（立体声轨再加左右平均，即送混响的那一路）。"""
    c = chunk
    yield from (c[0], c[2], c[3], c[2:4].mean(axis=0), c[1], c[4], c[5], c[4:6].mean(axis=0))
    yield from (c[6], c[7], c[8])
